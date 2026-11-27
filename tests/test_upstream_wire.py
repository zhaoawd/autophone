from __future__ import annotations

import base64
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

import pytest

from autophone.device import uds
from autophone.device.target import VPhoneTarget, VPhoneTargetBindingError, resolve_protocol, target_scope
from autophone.device.upstream_wire import decode, prepare


@contextmanager
def endpoint(response, *, delay=0, capabilities=None):
    with tempfile.TemporaryDirectory(prefix="upwire-", dir="/tmp") as directory:
        path = str(Path(directory) / "s")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        requests = []
        def serve():
            while True:
                peer, _ = server.accept()
                with peer:
                    data = b""
                    while b"\n" not in data:
                        block = peer.recv(4096)
                        if not block:
                            return
                        data += block
                    message = json.loads(data.split(b"\n")[0])
                    requests.append(message)
                    if message.get("t") == "capabilities":
                        advertised = capabilities if capabilities is not None else {"ok": True, "protocol": "upstream_v1", "guest_connected": True,
                                        "commands": {kind: True for kind in ["app_list", "app_launch", "tap", "type", "file_get", "file_put", "rpc", "screenshot"]}}
                        peer.sendall(json.dumps(advertised).encode() + b"\n")
                        continue
                    time.sleep(delay)
                    chunks = response(message) if callable(response) else response
                    for block in chunks:
                        try:
                            peer.sendall(block)
                        except BrokenPipeError:
                            break
                    return
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield path, requests
        finally:
            server.close()
            worker.join(timeout=2)
            assert not worker.is_alive()


def test_apps_mapping_and_fragmented_utf8_reply():
    data = json.dumps({"ok": True, "result": {"apps": [{"name": "中文", "data_path": "/var/mobile/test"}]}}, ensure_ascii=False).encode() + b"\n"
    with endpoint([bytes([value]) for value in data]) as (path, sent):
        result, sample = uds.request({"t": "app_list", "filter": "running", "screen": False}, sock_path=path, protocol="upstream_v1")
    assert sent == [{"t": "capabilities", "screen": False},
                    {"t": "rpc", "method": "apps.list", "params": {"filter": "running"}, "screen": False}]
    assert result["apps"][0]["data_container"] == "/var/mobile/test"
    assert result["apps"][0]["name"] == "中文"
    assert sample.cmd == "app_list"


def test_read_mapping_preserves_write_fence_classification(monkeypatch):
    monkeypatch.setattr("autophone.device.target.check_device_write", lambda *_: pytest.fail("read requested write fence"))
    with endpoint([b'{"ok":true,"result":{"apps":[]}}\n']) as (path, _):
        uds.request({"t": "app_list"}, sock_path=path, protocol="upstream_v1")


def test_failed_write_is_not_replayed_with_another_protocol():
    with endpoint([b'{"ok":false,"error":"unknown command: tap"}\n']) as (path, sent):
        result, _ = uds.request({"t": "tap", "x": 1, "y": 2}, sock_path=path, protocol="upstream_v1")
    assert result["ok"] is False
    assert [m["t"] for m in sent] == ["capabilities", "tap"]


@pytest.mark.parametrize("body", [b'{"ok":true}', b'{"ok":true}\n{}\n', b'[]\n', b'{"ok":1}\n'])
def test_malformed_or_incomplete_reply_is_refused(body):
    with endpoint([body]) as (path, _):
        with pytest.raises(uds.ProtocolError):
            uds.request({"t": "ping"}, sock_path=path, protocol="upstream_v1")


def test_frame_limit_checked_before_connect():
    with pytest.raises(uds.ProtocolError, match="request exceeds"):
        uds.request({"t": "type", "text": "x" * (1 << 20)}, sock_path="/missing", protocol="upstream_v1")


def test_response_limit():
    with endpoint([b'{"ok":true,"padding":"' + b'x' * (1 << 20) + b'"}\n']) as (path, _):
        with pytest.raises(uds.ProtocolError, match="response exceeds"):
            uds.request({"t": "ping"}, sock_path=path, protocol="upstream_v1")


def test_timeout_closes_connection_without_replay():
    with endpoint([b'{"ok":true}\n'], delay=.1) as (path, sent):
        with pytest.raises(TimeoutError) as error:
            uds.request({"t": "type", "text": "once"}, sock_path=path, protocol="upstream_v1", timeout=.03)
        assert error.value.operation_may_continue is True
    assert [m["t"] for m in sent] == ["capabilities", "type"]


def test_protocol_binding_cannot_cross_socket_or_change_mode(tmp_path):
    target = VPhoneTarget(tmp_path, tmp_path, tmp_path / "a.sock", "rig", protocol="upstream_v1")
    with target_scope(target):
        assert resolve_protocol(target.sock) == "upstream_v1"
        with pytest.raises(VPhoneTargetBindingError):
            resolve_protocol(tmp_path / "b.sock")
        with pytest.raises(VPhoneTargetBindingError):
            resolve_protocol(target.sock, "legacy")


def test_unknown_upstream_command_never_uses_legacy_wire():
    with pytest.raises(uds.ProtocolError, match="unsupported upstream"):
        uds.request({"t": "nonexistent_upstream_command"}, sock_path="/missing", protocol="upstream_v1")


def test_screenshot_suppresses_implicit_host_save():
    with prepare({"t": "screenshot"}) as (wire, _):
        assert wire == {"t": "screenshot", "save": False}


def test_inline_file_upload_keeps_bytes_and_permissions():
    with prepare({"t": "file_put", "path": "/var/tmp/test", "data_b64": base64.b64encode(b"bytes").decode(), "perm": "755"}) as (wire, finish):
        assert wire["mode"] == "755"
        assert Path(wire["local"]).read_bytes() == b"bytes"
        assert finish({"ok": True, "result": {"size": 5}})["size"] == 5


def test_inline_file_download():
    with prepare({"t": "file_get", "path": "/var/tmp/test"}) as (wire, finish):
        Path(wire["local"]).write_bytes(b"file")
        assert finish({"ok": True, "result": {"size": 4}})["data_b64"] == "ZmlsZQ=="


def test_shell_contract_maps_exit_and_truncation():
    reply = {"ok": True, "result": {"stdout": "out", "stderr": "err", "exit_code": 7,
             "timed_out": False, "stdout_truncated": False, "stderr_truncated": True}}
    result = decode("shell", reply)
    assert result["code"] == 7 and result["truncated"]
    with pytest.raises(ValueError):
        decode("shell", {"ok": True, "result": {"stdout": "partial"}})


def test_health_guest_timeout_keeps_host_only():
    from autophone.device.session import probe_uds_state
    calls = []
    def send(message, **kwargs):
        calls.append(message["t"])
        if message["t"] == "capabilities":
            return {"ok": True, "protocol": "upstream_v1", "guest_connected": True}, None
        raise TimeoutError("guest stalled")
    result = probe_uds_state("/fake.sock", protocol="upstream_v1", request_fn=send)
    assert result["state"] == "host_only" and not result["available"]
    assert calls == ["capabilities", "app_list"]


def test_health_disconnected_guest_never_sends_apps_request():
    from autophone.device.session import probe_uds_state
    calls = []
    def send(message, **kwargs):
        calls.append(message["t"])
        return {"ok": True, "protocol": "upstream_v1", "guest_connected": False}, None
    result = probe_uds_state("/fake.sock", protocol="upstream_v1", request_fn=send)
    assert result["state"] == "host_only" and calls == ["capabilities"]


@pytest.mark.parametrize("body", [b'', b'{"ok":true}', b'{"ok":true}\n{}\n', b'[]\n', b'{"ok":1}\n', b'\xff\n'])
def test_invalid_write_reply_preserves_unknown_outcome_without_replay(body):
    with endpoint([body]) as (path, sent):
        with pytest.raises(uds.ProtocolError) as error:
            uds.request({"t": "tap", "x": 1, "y": 2}, sock_path=path, protocol="upstream_v1")
        assert error.value.operation_may_continue is True
        assert error.value.command == "tap"
    assert [message["t"] for message in sent] == ["capabilities", "tap"]


@pytest.mark.parametrize(("advertised", "captured", "preview", "accepted"), [
    ({}, {}, (430, 932), True),
    ({}, {}, (669, 951), False),
    ({"screen": {"width": 1290, "height": 2796, "scale": 3}}, {}, (430, 932), True),
    ({"screen": {"width": 1398, "height": 2034, "scale": 3}}, {}, (466, 678), True),
    ({"screen": {"width": 1398, "height": 2034, "scale": 3}},
     {"screen": {"width": 2007, "height": 2853, "scale": 3}}, (669, 951), True),
    ({}, {"screen": {"width": 2007, "height": 2853, "scale": 3}}, (466, 678), False),
    ({}, {"screen": {"width": 2008, "height": 2855, "scale": 3}}, (669, 951), True),
    ({"screen": {"width": 1290, "height": 2796, "scale": 3}},
     {"screen": {"width": True, "height": 2796, "scale": 3}}, (430, 932), False),
    ({"screen": {"width": 1290, "height": 2796, "scale": 0}}, {}, (430, 932), False),
    ({"screen": None}, {}, (430, 932), False),
    ({}, {}, None, False),
])
def test_screenshot_checks_panel_geometry(advertised, captured, preview, accepted):
    from io import BytesIO
    from PIL import Image

    capabilities = {"ok": True, "protocol": "upstream_v1", "guest_connected": True,
                    "commands": {"screenshot": True}, **advertised}
    reply = {"ok": True, "result": captured}
    if preview:
        buffer = BytesIO()
        Image.new("RGB", preview).save(buffer, format="JPEG")
        reply["image"] = base64.b64encode(buffer.getvalue()).decode()
    with endpoint([json.dumps(reply).encode() + b"\n"], capabilities=capabilities) as (path, _):
        if accepted:
            _, sample = uds.request({"t": "screenshot"}, protocol="upstream_v1", sock_path=path, parse_image=False)
            assert (sample.image_w, sample.image_h) == preview
        else:
            with pytest.raises(uds.GeometryDrift):
                uds.request({"t": "screenshot"}, protocol="upstream_v1", sock_path=path, parse_image=False)


@pytest.mark.parametrize("command", ["screenshot", "location_current", "location_source_status"])
def test_read_only_request_failure_skips_write_guard_and_unknown_write_outcome(monkeypatch, command):
    monkeypatch.setattr("autophone.device.target.check_device_write",
                        lambda _: pytest.fail("read-only command entered write guard"))
    capabilities = {"ok": True, "protocol": "upstream_v1", "guest_connected": True,
                    "commands": {command: True, "rpc": True},
                    "guest_capabilities": ["location", "location_owned"]}
    with endpoint([b"invalid\n"], capabilities=capabilities) as (path, sent):
        with pytest.raises(uds.ProtocolError) as error:
            uds.request({"t": command}, sock_path=path, protocol="upstream_v1")
        assert not getattr(error.value, "operation_may_continue", False)
        assert len(sent) == 2


def test_invalid_location_readback_is_not_an_unconfirmed_write():
    capabilities = {"ok": True, "protocol": "upstream_v1", "guest_connected": True,
                    "commands": {"rpc": True}, "guest_capabilities": ["location"]}
    with endpoint([b'{"ok":true,"result":{}}\n'], capabilities=capabilities) as (path, _):
        with pytest.raises(uds.ProtocolError) as error:
            uds.request({"t": "location_current"}, sock_path=path, protocol="upstream_v1")
        assert not getattr(error.value, "operation_may_continue", False)
