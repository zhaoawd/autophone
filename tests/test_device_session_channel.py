"""控制面交互通道（BL-100）：会话租约、路由收口与真 WebSocket 往返。

判据对应关系：

- 「11 条路由在控制面已配置时不再于 web 进程内驱动设备」→
  `test_ten_routes_never_touch_the_local_device_when_the_control_plane_is_configured`；
- 「一条会话在多次动作之间持有同一条 ScreenLock lease」→
  `test_one_console_session_holds_one_lease_across_actions`；
- 单次往返延迟 → `tests/support/device_session_harness.py` 的 harness 证明
  真 uvicorn + 真 WebSocket + 真 HTTP 这条链路通。
"""
from __future__ import annotations

import base64
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from autophone.application.device_session import (
    DeviceSessionOps,
    DeviceSessionRefused,
    RemoteDeviceSessions,
    control_http_base_url,
)
from autophone.device.lock import ScreenLock
from autophone.adapters.http.app import dependencies as api
from autophone.application.device_console import DeviceConsoleManager
from autophone.rig.device_session import DeviceSessionAgent, device_session_ws_url


class _RegisteredControl:
    def __init__(self):
        self.generation = 2

    def get_host(self, host):
        assert host == "host"
        return {
            "rigs": [{"host_id": host, "rig_id": "rig", "generation": self.generation}],
            "targets": [{"host_id": host, "rig_id": "rig", "rig_generation": self.generation,
                         "vm_dir": "/vphone/vm-alt"}],
        }


class _GatewaySocket:
    def __init__(self, generation=2, devices=None, host="host", rig="rig"):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "hello", "host_id": host, "rig_id": rig,
                                  "rig_generation": generation, "devices": devices or ["rig"]})
        self.handshake = asyncio.Event()
        self.sent = []
        self.closed = False

    async def accept(self):
        pass

    async def receive_json(self):
        return await self.incoming.get()

    async def send_json(self, message):
        self.sent.append(message)
        if message["type"] in {"ready", "error"}:
            self.handshake.set()
        if message["type"] == "request":
            self.incoming.put_nowait({"request_id": message["request_id"], "ok": True,
                                     "result": {"device": message["device"]}})

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.parametrize("generation,devices,host,rig", [
    (1, ["rig"], "host", "rig"),
    (2, ["other-rig"], "host", "rig"),
    (2, ["rig"], "other-host", "rig"),
    (2, ["rig"], "host", "unregistered"),
    # vm-alt 按普通目录处理：目录名即设备键，不再映射到 vphone-0。
    (2, ["rig", "vphone-0"], "host", "rig"),
])
def test_gateway_rejects_unregistered_identity_and_device_keys(generation, devices, host, rig):
    from autophone.control.http import create_control_app

    async def check():
        gateway = create_control_app(_RegisteredControl()).state.device_session_gateway
        socket = _GatewaySocket(generation, devices, host, rig)
        task = asyncio.create_task(gateway.connect(socket))
        try:
            await asyncio.wait_for(socket.handshake.wait(), 1)
            assert socket.sent[0]["type"] == "error"
            assert await gateway.connected_devices() == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["console_start", "screen_holder"])
def test_gateway_rechecks_generation_before_forwarding_and_canonicalizes_alias(operation):
    from autophone.control.http import create_control_app

    async def check():
        control = _RegisteredControl()
        gateway = create_control_app(control).state.device_session_gateway
        socket = _GatewaySocket(devices=["rig", "vm-alt"])
        task = asyncio.create_task(gateway.connect(socket))
        try:
            await asyncio.wait_for(socket.handshake.wait(), 1)
            assert await gateway.call("vm-alt", operation, {}) == {"device": "rig"}
            control.generation += 1
            before = len(socket.sent)
            with pytest.raises(DeviceSessionRefused, match="identity"):
                await gateway.call("rig", "console_event", {})
            assert len(socket.sent) == before
            assert socket.closed
            assert await gateway.connected_devices() == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_delayed_old_handshake_does_not_replace_new_generation():
    from autophone.control.http import create_control_app

    async def check():
        captured = threading.Event()
        release = threading.Event()
        control = _RegisteredControl()
        control.generation = 1
        snapshot = control.get_host

        def delayed_snapshot(host):
            result = snapshot(host)
            if result["rigs"][0]["generation"] == 1:
                captured.set()
                assert release.wait(2)
            return result

        control.get_host = delayed_snapshot
        gateway = create_control_app(control).state.device_session_gateway
        old, current = _GatewaySocket(1), _GatewaySocket(2)
        tasks = [asyncio.create_task(gateway.connect(old))]
        try:
            assert await asyncio.to_thread(captured.wait, 1)
            control.generation = 2
            tasks.append(asyncio.create_task(gateway.connect(current)))
            await asyncio.wait_for(current.handshake.wait(), 1)
            release.set()
            await asyncio.wait_for(old.handshake.wait(), 1)
            assert old.sent[0]["type"] == "error"
            assert not current.closed
            assert (await gateway.connected_devices())[0]["rig_generation"] == 2
            assert await gateway.call("rig", "preview_frame", {}) == {"device": "rig"}
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(check())


def test_served_device_keys_retains_default_alias_after_agent_environment_binding(monkeypatch):
    from autophone.rig.main import served_device_keys

    monkeypatch.setenv("AP_DEVICE", "rig")
    monkeypatch.setenv("VM_DIR", "/vphone/vm-alt")
    target = SimpleNamespace(rig_id="rig", vm_dir=Path("/vphone/vm-alt"))
    assert served_device_keys(target) == ["rig", "vm-alt"]

PREVIEW_JPEG = b"\xff\xd8fake-frame"


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def screenshot(self) -> tuple[bytes, int, int]:
        return PREVIEW_JPEG, 430, 932

    def tap(self, px: int, py: int) -> None:
        self.calls.append(("tap", px, py))

    def swipe(self, px1, py1, px2, py2, *, ms) -> None:
        self.calls.append(("swipe", px1, py1, px2, py2, ms))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def press_key(self, name: str) -> None:
        self.calls.append(("key", name))


def _ops(tmp_path: Path) -> tuple[DeviceSessionOps, ScreenLock, _Driver]:
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    driver = _Driver()
    console = DeviceConsoleManager(
        lock=lock, driver_factory=lambda: driver, clock=lambda: 1000.0,
    )
    ops = DeviceSessionOps(
        console=console,
        screenshot=lambda: PREVIEW_JPEG,
        status=lambda device: {"device": device, "online": True, "queue_depth": 0},
        target=lambda: {"device": "vphone-local"},
    )
    return ops, lock, driver


def _tap_payload(ops: DeviceSessionOps, session: dict, event_id: str) -> dict:
    view = ops.call("vphone-local", "console_view", {"session_id": session["session_id"]})
    return {
        "session_id": session["session_id"],
        "controller_token": session["controller_token"],
        "event": {
            "event_id": event_id, "kind": "tap", "px": 100, "py": 200,
            "frame_seq": view["seq"],
        },
    }


# ── 判据 3：一条会话在多次动作之间持有同一条 lease ─────────────────────────────
def test_one_console_session_holds_one_lease_across_actions(tmp_path: Path):
    ops, lock, driver = _ops(tmp_path)
    session = ops.call("vphone-local", "console_start", {})

    opened = lock.status(device="vphone-local")
    assert opened["locked"] is True
    assert opened["owner_kind"] == "web_control"
    assert opened["owner_id"] == session["session_id"]

    holdings = []
    for index in range(4):
        ops.call("vphone-local", "console_event", _tap_payload(ops, session, f"e{index}"))
        # 每次动作**之后**立刻读锁：动作之间放开设备的实现会在这里露出 locked=False，
        # 或者换一个 owner_id（重新取了一条新 lease）。
        state = lock.status(device="vphone-local")
        holdings.append((state["locked"], state["owner_id"], state["acquired_at"]))

    assert len(driver.calls) == 4, "四次 tap 必须都真的打到驱动上"
    assert holdings == [(True, session["session_id"], opened["acquired_at"])] * 4

    # 另一个持有者在会话存活期间一律拿不到设备。
    assert lock.try_lease(device="vphone-local", owner_kind="run", owner_id="R1") is None

    ops.call("vphone-local", "console_stop", {
        "session_id": session["session_id"],
        "controller_token": session["controller_token"],
    })
    assert lock.status(device="vphone-local")["locked"] is False
    run_lease = lock.try_lease(device="vphone-local", owner_kind="run", owner_id="R1")
    assert run_lease is not None
    run_lease.release()


def test_console_frames_do_not_take_a_second_lease(tmp_path: Path):
    """帧读取走同一条会话，不额外取锁、不换 owner。"""
    ops, lock, _driver = _ops(tmp_path)
    session = ops.call("vphone-local", "console_start", {})
    before = lock.status(device="vphone-local")
    for _ in range(3):
        frame = ops.call("vphone-local", "console_frame", {"session_id": session["session_id"]})
        assert base64.b64decode(frame["jpeg_b64"]) == PREVIEW_JPEG
    assert lock.status(device="vphone-local") == before


def test_preview_frames_reuse_one_session_and_never_take_a_lease(tmp_path: Path):
    ops, lock, _driver = _ops(tmp_path)
    frames = [ops.call("vphone-local", "preview_frame", {}) for _ in range(5)]
    assert len({frame["preview_session_id"] for frame in frames}) == 1, "每帧不得新建会话"
    assert [frame["seq"] for frame in frames] == [1] * 5
    # 预览不持 lease：run 随时能拿到设备（今天的 screen.jpg 也是这个语义）。
    lease = lock.try_lease(device="vphone-local", owner_kind="run", owner_id="R1")
    assert lease is not None
    lease.release()


# ── 判据 2：控制面已配置时，web 进程不驱动设备 ─────────────────────────────────
TEN_ROUTED_CALLS = (
    ("device_status", lambda: api.device_status("rig")),
    ("preview_frame", lambda: api.screen_frame("rig")),
    ("preview_frame", lambda: api.screen_stream("rig", interval_ms=800)),
    ("console_device_status", lambda: api.device_console_status("rig")),
    ("console_start", lambda: api.device_console_start("rig")),
    ("console_view", lambda: api.device_console_view("rig", "ctl-1")),
    ("console_frame", lambda: api.device_console_frame("rig", "ctl-1")),
    ("console_event", lambda: api.device_console_event(
        "rig", "ctl-1", _request(), {"kind": "tap"},
    )),
    ("console_heartbeat", lambda: api.device_console_heartbeat("rig", "ctl-1", _request())),
    ("console_stop", lambda: api.device_console_stop("rig", "ctl-1", _request())),
)


def _request():
    return api.Request({"type": "http", "headers": [(b"x-controller-token", b"tok")]})


class _RecordingSessions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def call(self, device: str, op: str, payload: dict) -> dict:
        self.calls.append((device, op))
        return {
            "jpeg_b64": base64.b64encode(PREVIEW_JPEG).decode("ascii"),
            "seq": 1,
            "captured_at": 1.0,
            "session_id": "ctl-1",
            "device": device,
            "ok": True,
        }


def _explode(*_args, **_kwargs):
    raise AssertionError("web 进程在控制面模式下不得驱动设备")


def test_ten_routes_never_touch_the_local_device_when_the_control_plane_is_configured(monkeypatch):
    recorder = _RecordingSessions()
    monkeypatch.setattr(api, "_configured_control_or_http_error", lambda: object())
    monkeypatch.setattr(api, "RemoteDeviceSessions", lambda *_a, **_k: recorder)
    # 本进程的设备面全部改成炸弹：任何一条路由碰它就是判据不成立。
    monkeypatch.setattr(api.local_device_sessions, "call", _explode)
    monkeypatch.setattr(api.device_console, "start", _explode)
    monkeypatch.setattr(api.device_console, "frame", _explode)
    monkeypatch.setattr(api.device_console, "event", _explode)
    monkeypatch.setattr(api.device_application, "screenshot", _explode)
    monkeypatch.setattr(api.device_application, "target", _explode)
    monkeypatch.setattr(api.service, "status", _explode)

    for _expected_op, invoke in TEN_ROUTED_CALLS:
        invoke()

    assert [op for _device, op in recorder.calls] == [op for op, _ in TEN_ROUTED_CALLS]
    assert {device for device, _op in recorder.calls} == {"rig"}


def test_compat_path_still_executes_in_the_web_process(monkeypatch):
    monkeypatch.setattr(api, "_configured_control_or_http_error", lambda: None)
    assert api._device_sessions("rig") is api.local_device_sessions


def test_control_plane_configured_selects_the_remote_client(monkeypatch):
    monkeypatch.setattr(api, "_configured_control_or_http_error", lambda: object())
    # 本例要的是「由 Agent WebSocket 地址推导」这条分支，所以显式键必须不在环境里
    # （conftest 的 isolate_control_plane_env 会设一个连不上的 AUTOPHONE_CONTROL_HTTP）。
    monkeypatch.delenv("AUTOPHONE_CONTROL_HTTP", raising=False)
    monkeypatch.setenv("AUTOPHONE_CONTROL_WS", "ws://10.0.0.9:8100/api/control/agent/ws")
    sessions = api._device_sessions("rig")
    assert isinstance(sessions, RemoteDeviceSessions)
    assert sessions.base_url == "http://10.0.0.9:8100"


def test_refusals_keep_their_status_and_code_through_the_route(monkeypatch):
    class _Busy:
        def call(self, device, op, payload):
            raise DeviceSessionRefused("device is busy: run R1", code="device_busy")

    monkeypatch.setattr(api, "_configured_control_or_http_error", lambda: object())
    monkeypatch.setattr(api, "RemoteDeviceSessions", lambda *_a, **_k: _Busy())
    with pytest.raises(api.HTTPException) as exc:
        api.device_console_start("rig")
    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "device_busy", "message": "device is busy: run R1"}


# ── 会话终止与 lease 释放 ─────────────────────────────────────────────────────
class _FakeSocket:
    """一条可编排的 WebSocket 替身：先握手，再按脚本回消息或抛断线。"""

    def __init__(self, script: list) -> None:
        self.sent: list[dict] = []
        self._script = list(script)
        self.closed = False

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self, timeout: float | None = None):
        if not self._script:
            raise ConnectionError("device session channel closed by peer")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def close(self) -> None:
        self.closed = True


def test_rig_disconnect_releases_every_console_lease(tmp_path: Path):
    ops, lock, _driver = _ops(tmp_path)
    session = ops.call("vphone-local", "console_start", {})
    assert lock.status(device="vphone-local")["locked"] is True

    socket = _FakeSocket([
        {"type": "ready", "devices": ["vphone-local"]},
        ConnectionError("peer went away"),
    ])
    attempts: list[int] = []

    def connect(_url, _timeout):
        attempts.append(1)
        if len(attempts) > 1:  # 第二轮不再重连，让 run_forever 收场
            agent.stop()
            raise ConnectionError("control plane still down")
        return socket

    agent = DeviceSessionAgent(
        "ws://127.0.0.1:8100/api/control/device-session/ws",
        host_id="h", rig_id="vphone-local", rig_generation=1,
        devices=["vphone-local"], ops=ops,
        connect=connect,
        reconnect_delay_s=0.01,
    )
    agent.run_forever()

    assert socket.closed is True
    assert lock.status(device="vphone-local")["locked"] is False, "掉线必须还掉 lease"
    lease = lock.try_lease(device="vphone-local", owner_kind="run", owner_id="R1")
    assert lease is not None
    lease.release()
    # 会话本身也不再存在：重连后旧 session_id 不能继续驱动设备。
    with pytest.raises(DeviceSessionRefused) as exc:
        ops.call("vphone-local", "console_view", {"session_id": session["session_id"]})
    assert exc.value.status in (404, 410)


def test_agent_answers_one_request_and_carries_a_refusal_back(tmp_path: Path):
    ops, _lock, driver = _ops(tmp_path)
    session = ops.call("vphone-local", "console_start", {})
    socket = _FakeSocket([
        {"type": "ready", "devices": ["vphone-local"]},
        {
            "type": "request", "request_id": "r1", "device": "vphone-local",
            "op": "console_event", "payload": _tap_payload(ops, session, "e0"),
        },
        {
            "type": "request", "request_id": "r2", "device": "vphone-local",
            "op": "console_view", "payload": {},
        },
        ConnectionError("done"),
    ])
    agent = DeviceSessionAgent(
        "ws://127.0.0.1:8100/api/control/device-session/ws",
        host_id="h", rig_id="vphone-local", rig_generation=1,
        devices=["vphone-local"], ops=ops,
        connect=lambda _url, _timeout: socket,
    )
    with pytest.raises(ConnectionError):
        agent.run_once()

    hello, ok_reply, refusal = socket.sent
    assert hello["type"] == "hello" and hello["devices"] == ["vphone-local"]
    assert ok_reply == {"type": "result", "request_id": "r1", "ok": True, "result": {
        "event_id": "e0", "index": 0, "kind": "tap", "ok": True,
        "action_at": ok_reply["result"]["action_at"],
    }}
    assert driver.calls == [("tap", 100, 200)]
    assert refusal["ok"] is False
    assert refusal["error"]["status"] == 422
    assert refusal["error"]["code"] == "validation"


def test_unknown_op_is_refused_on_both_sides(tmp_path: Path):
    ops, _lock, _driver = _ops(tmp_path)
    with pytest.raises(DeviceSessionRefused) as local:
        ops.call("vphone-local", "console_teleport", {})
    assert local.value.status == 422 and local.value.code == "unknown_op"
    with pytest.raises(DeviceSessionRefused) as remote:
        RemoteDeviceSessions("http://127.0.0.1:1").call("d", "console_teleport", {})
    assert remote.value.code == "unknown_op"


def test_remote_client_reports_an_unreachable_control_plane_as_503():
    with pytest.raises(DeviceSessionRefused) as exc:
        RemoteDeviceSessions("http://127.0.0.1:1", timeout_s=1.0).call("d", "device_status", {})
    assert exc.value.status == 503
    assert exc.value.code == "device_session_unavailable"


def test_control_http_base_url_prefers_explicit_then_derives_from_agent_ws():
    assert control_http_base_url({"AUTOPHONE_CONTROL_HTTP": "http://c:9/"}) == "http://c:9"
    assert control_http_base_url(
        {"AUTOPHONE_CONTROL_WS": "ws://127.0.0.1:8100/api/control/agent/ws"},
    ) == "http://127.0.0.1:8100"
    assert control_http_base_url(
        {"AUTOPHONE_CONTROL_WS": "wss://rig.example:443/api/control/agent/ws"},
    ) == "https://rig.example:443"
    assert control_http_base_url({}) == "http://127.0.0.1:8100"


def test_device_session_ws_url_keeps_the_control_plane_endpoint():
    assert device_session_ws_url("ws://127.0.0.1:8100/api/control/agent/ws") == (
        "ws://127.0.0.1:8100/api/control/device-session/ws"
    )
    with pytest.raises(ValueError):
        device_session_ws_url("/api/control/agent/ws")


# ── 真 uvicorn + 真 WebSocket + 真 HTTP 的一次往返 ────────────────────────────
def test_channel_round_trip_over_a_real_websocket():
    from tests.support.device_session_harness import ChannelHarness

    with ChannelHarness() as harness:
        client = harness.client()
        opened = client.call(harness.device, "console_start", {})
        assert opened["session_id"] == "ctl-bench"
        frame = client.call(harness.device, "preview_frame", {})
        assert base64.b64decode(frame["jpeg_b64"])[:2] == b"\xff\xd8"
        first = client.call(harness.device, "preview_frame", {})["preview_session_id"]
        second = client.call(harness.device, "preview_frame", {})["preview_session_id"]
        assert first == second, "帧类读取复用同一条预览会话"
        # 没有 rig 在服务的设备一律 503，不静默落回本机。
        with pytest.raises(DeviceSessionRefused) as exc:
            client.call("no-such-rig", "device_status", {})
        assert exc.value.status == 503
        assert exc.value.code == "device_session_unavailable"


def test_preview_concurrency_cache_expiry_and_failure(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from autophone.application.device_console import FRAME_MIN_INTERVAL_S
    from autophone.application.device_session import PREVIEW_MIN_INTERVAL_S

    assert PREVIEW_MIN_INTERVAL_S == FRAME_MIN_INTERVAL_S == 0.12
    ops, lock, _ = _ops(tmp_path)
    now = [1000.0]
    ops.clock = lambda: now[0]
    entered, release = threading.Event(), threading.Event()
    attempts = threading.Barrier(9)
    calls = []

    def capture():
        calls.append('capture')
        entered.set()
        assert release.wait(3)
        now[0] = 1001.0
        return PREVIEW_JPEG

    ops._screenshot = capture

    def fetch():
        attempts.wait(timeout=3)
        return ops.call('rig', 'preview_frame', {})

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch) for _ in range(8)]
        attempts.wait(timeout=3)
        assert entered.wait(3)
        time.sleep(0.05)
        release.set()
        frames = [f.result(timeout=3) for f in futures]
    assert calls == ['capture']
    assert all(frame == frames[0] for frame in frames)
    assert frames[0]['seq'] == 1 and frames[0]['captured_at'] == 1001.0
    assert not lock.status(device='rig')['locked']
    now[0] += 0.121
    ops._screenshot = lambda: (_ for _ in ()).throw(OSError('capture failed'))
    with pytest.raises(DeviceSessionRefused, match='capture failed'):
        ops.call('rig', 'preview_frame', {})
    ops._screenshot = lambda: b'recovered'
    assert ops.call('rig', 'preview_frame', {})['seq'] == 2
    now[0] += ops.preview_idle_ttl_s + 1
    renewed = ops.call('rig', 'preview_frame', {})
    assert renewed['seq'] == 1
    assert renewed['preview_session_id'] != frames[0]['preview_session_id']
    ops.close_all('test_done')


def test_preview_capture_does_not_block_another_device(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ops, _, _ = _ops(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def capture():
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return PREVIEW_JPEG

    ops._screenshot = capture
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ops.call, 'rig-one', 'preview_frame', {})
            assert entered.wait(3)
            second = pool.submit(ops.call, 'rig-two', 'preview_frame', {})
            assert second.result(timeout=1)['seq'] == 1
            release.set()
            assert first.result(timeout=3)['seq'] == 1
    finally:
        release.set()
        ops.close_all('test_done')


def test_jpeg_and_mjpeg_share_the_device_preview_cache(tmp_path, monkeypatch):
    ops, _, _ = _ops(tmp_path)
    now = [1000.0]
    ops.clock = lambda: now[0]
    calls = []
    ops._screenshot = lambda: calls.append('capture') or PREVIEW_JPEG
    monkeypatch.setattr(api, '_device_session_call', ops.call)
    monkeypatch.setattr(api, '_device_sessions', lambda device: ops)
    still = api.screen_frame('rig')
    stream = api._preview_stream('rig', interval_ms=0)
    try:
        assert still.body == PREVIEW_JPEG
        assert PREVIEW_JPEG in next(stream)
        assert calls == ['capture']
        now[0] += 0.121
        assert PREVIEW_JPEG in next(stream)
        assert len(calls) == 2
    finally:
        stream.close()
        ops.close_all('test_done')
