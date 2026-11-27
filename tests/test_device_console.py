from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from autophone.adapters.http.app import dependencies as api
from autophone.application.device_console import (
    DeviceConsoleError,
    DeviceConsoleManager,
    DeviceUnavailable,
)
from autophone.device.lock import ScreenLock, ScreenLockBusy
from autophone.device.uds import HARDWARE_KEYS

PREVIEW_JPEG = b"\xff\xd8fake-frame"


class FakeDriver:
    """Records every dispatch so "did the device move?" is directly assertable."""

    def __init__(self, *, dims: tuple[int, int] = (430, 932), fail: bool = False) -> None:
        self.dims = dims
        self.fail = fail
        self.calls: list[tuple] = []
        self.before_action: threading.Event | None = None
        self.release_action: threading.Event | None = None

    def screenshot(self) -> tuple[bytes, int, int]:
        return PREVIEW_JPEG, self.dims[0], self.dims[1]

    def _act(self, call: tuple) -> None:
        if self.before_action is not None:
            self.before_action.set()
        if self.release_action is not None:
            self.release_action.wait(timeout=5)
        if self.fail:
            raise DeviceUnavailable("tap not ok: 'device asleep'")
        self.calls.append(call)

    def tap(self, px: int, py: int) -> None:
        self._act(("tap", px, py))

    def swipe(self, px1: int, py1: int, px2: int, py2: int, *, ms: int) -> None:
        self._act(("swipe", px1, py1, px2, py2, ms))

    def type_text(self, text: str) -> None:
        self._act(("type", text))

    def press_key(self, name: str) -> None:
        self._act(("key", name))


def _manager(tmp_path: Path, driver: FakeDriver | None = None, *, clock=None) -> tuple[DeviceConsoleManager, FakeDriver]:
    drv = driver or FakeDriver()
    manager = DeviceConsoleManager(
        lock=ScreenLock(root=tmp_path / "runs/.locks"),
        driver_factory=lambda: drv,
        clock=clock or (lambda: 1000.0),
    )
    return manager, drv


def _tap(manager: DeviceConsoleManager, session: dict, *, event_id: str, px: int = 100, py: int = 200, seq: int | None = None) -> dict:
    payload = {
        "event_id": event_id, "kind": "tap", "px": px, "py": py,
        "frame_seq": seq if seq is not None else manager.view(session["session_id"])["seq"],
    }
    return manager.event(session["session_id"], payload, session["controller_token"])


def test_console_drives_device_and_never_writes_an_artifact(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")

    ack = _tap(manager, session, event_id="e1")

    assert ack["ok"] is True and ack["kind"] == "tap" and ack["index"] == 0
    assert driver.calls == [("tap", 100, 200)]
    # 纯操作会话的全部价值就在于它不留资产：没有录制目录、没有 trace。
    assert not (tmp_path / "runs" / "rec").exists()
    assert sorted(p.name for p in (tmp_path / "runs/.locks").iterdir()) == [
        "vphone-local.json", "vphone-local.lock",
    ]


def test_replayed_event_id_returns_the_same_ack_without_a_second_dispatch(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")

    first = _tap(manager, session, event_id="e1")
    again = _tap(manager, session, event_id="e1")

    assert again == first
    assert driver.calls == [("tap", 100, 200)]


def test_reused_event_id_with_a_different_intent_is_refused(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")
    _tap(manager, session, event_id="e1", px=100, py=200)

    with pytest.raises(DeviceConsoleError) as exc:
        _tap(manager, session, event_id="e1", px=101, py=200)

    assert exc.value.code == "idempotency_conflict"
    assert driver.calls == [("tap", 100, 200)]


def test_a_failed_dispatch_burns_the_event_id_instead_of_gambling_a_retry(tmp_path: Path):
    manager, driver = _manager(tmp_path, FakeDriver(fail=True))
    session = manager.start("vphone-local")

    with pytest.raises(DeviceConsoleError) as exc:
        _tap(manager, session, event_id="e1")
    assert exc.value.status == 503 and exc.value.code == "device_error"

    driver.fail = False
    ack = _tap(manager, session, event_id="e1")
    assert ack["ok"] is False and "device asleep" in ack["error"]
    assert driver.calls == []  # 同一个 id 绝不第二次落到设备上
    assert manager.view(session["session_id"])["action_count"] == 0


def test_a_stale_frame_cannot_be_tapped(tmp_path: Path):
    now = {"t": 1000.0}
    manager, driver = _manager(tmp_path, clock=lambda: now["t"])
    session = manager.start("vphone-local")
    seq = manager.view(session["session_id"])["seq"]

    now["t"] += manager.frame_max_age_s + 1
    with pytest.raises(DeviceConsoleError) as exc:
        _tap(manager, session, event_id="e1", seq=seq)

    assert exc.value.code == "stale_frame"
    assert driver.calls == []


def test_only_the_controller_can_drive_the_session(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")

    with pytest.raises(DeviceConsoleError) as exc:
        manager.event(
            session["session_id"],
            {"event_id": "e1", "kind": "tap", "px": 10, "py": 10, "frame_seq": 1},
            "somebody-elses-token",
        )

    assert exc.value.code == "token_mismatch"
    assert driver.calls == []


def test_one_action_in_flight_per_session(tmp_path: Path):
    driver = FakeDriver()
    driver.before_action = threading.Event()
    driver.release_action = threading.Event()
    manager, _ = _manager(tmp_path, driver)
    session = manager.start("vphone-local")
    errors: list[Exception] = []

    slow = threading.Thread(target=lambda: _tap(manager, session, event_id="e1", seq=1))
    slow.start()
    assert driver.before_action.wait(timeout=5)
    try:
        with pytest.raises(DeviceConsoleError) as exc:
            _tap(manager, session, event_id="e2", px=300, py=300, seq=1)
        assert exc.value.code == "busy"
    finally:
        driver.release_action.set()
        slow.join(timeout=5)
    assert not errors
    assert driver.calls == [("tap", 100, 200)]


def test_console_and_runs_are_mutually_exclusive_without_queueing(tmp_path: Path):
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(lock=lock, driver_factory=FakeDriver, clock=lambda: 1000.0)
    session = manager.start("vphone-local")

    # A run must fail fast rather than silently queue behind the person who is
    # currently holding the screen in their browser.
    with pytest.raises(ScreenLockBusy):
        with lock.acquire(device="vphone-local", run_id="R0001", blocking=True):
            pass

    manager.stop(session["session_id"], session["controller_token"])
    with lock.acquire(device="vphone-local", run_id="R0001", blocking=True):
        pass


def test_stop_waits_for_the_inflight_action_before_handing_the_device_back(tmp_path: Path):
    driver = FakeDriver()
    driver.before_action = threading.Event()
    driver.release_action = threading.Event()
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(
        lock=lock, driver_factory=lambda: driver, clock=lambda: 1000.0,
    )
    session = manager.start("vphone-local")

    slow = threading.Thread(target=lambda: _tap(manager, session, event_id="e1", seq=1))
    slow.start()
    assert driver.before_action.wait(timeout=5)
    stopper = threading.Thread(
        target=lambda: manager.stop(session["session_id"], session["controller_token"]),
    )
    stopper.start()
    try:
        # 动作还在飞：lease 必须还在手上。提前还锁 = 下一个 run 拿到设备后
        # 这一下 tap 打在它身上。
        time.sleep(0.05)
        assert lock.status(device="vphone-local").get("locked") is True
    finally:
        driver.release_action.set()
        slow.join(timeout=5)
        stopper.join(timeout=5)

    assert driver.calls == [("tap", 100, 200)]
    assert lock.status(device="vphone-local").get("locked") is False


def test_heartbeat_expiry_also_waits_for_the_inflight_action(tmp_path: Path):
    now = {"t": 1000.0}
    driver = FakeDriver()
    driver.before_action = threading.Event()
    driver.release_action = threading.Event()
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(
        lock=lock, driver_factory=lambda: driver, clock=lambda: now["t"],
    )
    session = manager.start("vphone-local")

    slow = threading.Thread(target=lambda: _tap(manager, session, event_id="e1", seq=1))
    slow.start()
    assert driver.before_action.wait(timeout=5)
    now["t"] += manager.heartbeat_ttl_s + 1
    try:
        manager.expire_heartbeats()  # 在飞：这一轮不收，等下一轮扫描
        assert manager.view(session["session_id"])["state"] == "live"
    finally:
        driver.release_action.set()
        slow.join(timeout=5)

    # 动作落地本身算一次活着（会刷新心跳），再空转过 TTL 才该收回设备。
    now["t"] += manager.heartbeat_ttl_s + 1
    manager.expire_heartbeats()
    assert lock.status(device="vphone-local").get("locked") is False


def test_a_session_is_reachable_only_under_its_own_device(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")
    sid, token = session["session_id"], session["controller_token"]

    # 会话按 session_id 寻址，但挂在 /api/devices/{device}/… 下面：路径设备不符
    # 就当没这条会话，别让 URL 说的设备和真正被驱动的设备是两台。
    for call in (
        lambda: manager.view(sid, device="other-rig"),
        lambda: manager.frame(sid, device="other-rig"),
        lambda: manager.heartbeat(sid, token, device="other-rig"),
        lambda: manager.stop(sid, token, device="other-rig"),
        lambda: manager.event(
            sid, {"event_id": "e1", "kind": "tap", "px": 10, "py": 10, "frame_seq": 1},
            token, device="other-rig",
        ),
    ):
        with pytest.raises(DeviceConsoleError) as exc:
            call()
        assert exc.value.status == 404 and exc.value.code == "not_found"

    assert driver.calls == []
    assert manager.view(sid, device="vphone-local")["state"] == "live"


def test_console_start_refuses_a_device_another_owner_holds(tmp_path: Path):
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(lock=lock, driver_factory=FakeDriver, clock=lambda: 1000.0)
    lease = lock.try_lease(device="vphone-local", owner_kind="run", owner_id="R0007")

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local")
    assert exc.value.code == "device_busy" and "R0007" in str(exc.value)

    lease.release()
    assert manager.start("vphone-local")["state"] == "live"


def test_console_start_is_blocked_by_a_desktop_recording_lease_not_its_sidecar(tmp_path: Path):
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(lock=lock, driver_factory=FakeDriver, clock=lambda: 1000.0)
    # sidecar 只是展示：陈旧的 holder 文件不该拦住任何人（以前它是唯一证据，只能拦）。
    lock.mark_recording(
        device="vphone-local", recording_id="runs/rec/demo", pid=1, surface="desktop",
    )
    stale_ok = manager.start("vphone-local")
    manager.stop(stale_ok["session_id"], stale_ok["controller_token"])

    # 真正在跑的桌面录制持的是真 lease，这才该把远程操作挡在外面。
    lease = lock.try_lease(
        device="vphone-local", owner_kind="desktop_recording", owner_id="runs/rec/demo",
    )
    assert lease is not None
    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local")
    assert exc.value.code == "device_busy" and "desktop_recording" in str(exc.value)

    lease.release()
    assert manager.start("vphone-local")["state"] == "live"


def test_unexpected_preview_dimensions_fail_closed_and_release_the_device(tmp_path: Path):
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(
        lock=lock, driver_factory=lambda: FakeDriver(dims=(390, 844)), clock=lambda: 1000.0,
    )

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local")

    assert exc.value.code == "geometry_error" and exc.value.status == 503
    assert lock.status(device="vphone-local").get("locked") is False


def test_an_abandoned_browser_releases_the_device(tmp_path: Path):
    now = {"t": 1000.0}
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(
        lock=lock, driver_factory=FakeDriver, clock=lambda: now["t"],
    )
    session = manager.start("vphone-local")

    now["t"] += manager.heartbeat_ttl_s / 2
    manager.heartbeat(session["session_id"], session["controller_token"])
    manager.expire_heartbeats()
    assert manager.view(session["session_id"])["state"] == "live"

    now["t"] += manager.heartbeat_ttl_s + 1
    manager.expire_heartbeats()

    with pytest.raises(DeviceConsoleError) as exc:
        manager.view(session["session_id"])
    assert exc.value.status == 410 and exc.value.code == "session_closed"
    assert lock.status(device="vphone-local").get("locked") is False
    assert manager.start("vphone-local")["state"] == "live"


def test_device_status_reports_who_holds_the_screen(tmp_path: Path):
    manager, _ = _manager(tmp_path)
    assert manager.device_status("vphone-local") == {"device": "vphone-local", "active": False}

    session = manager.start("vphone-local")
    _tap(manager, session, event_id="e1")
    status = manager.device_status("vphone-local")
    assert status["active"] is True and status["action_count"] == 1

    manager.stop(session["session_id"], session["controller_token"])
    assert manager.device_status("vphone-local")["active"] is False


def test_swipe_and_type_reach_the_device_verbatim(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")
    sid, token = session["session_id"], session["controller_token"]

    manager.event(sid, {
        "event_id": "s1", "kind": "swipe", "px1": 215, "py1": 700,
        "px2": 215, "py2": 300, "ms": 320, "frame_seq": manager.view(sid)["seq"],
    }, token)
    manager.event(sid, {
        "event_id": "t1", "kind": "type", "text": "13800000000",
        "frame_seq": manager.view(sid)["seq"],
    }, token)

    assert driver.calls == [("swipe", 215, 700, 215, 300, 320), ("type", "13800000000")]


def test_hardware_keys_reach_the_device_verbatim(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")
    sid, token = session["session_id"], session["controller_token"]

    for i, name in enumerate(HARDWARE_KEYS):
        manager.event(sid, {
            "event_id": f"k{i}", "kind": "key", "name": name,
            "frame_seq": manager.view(sid)["seq"],
        }, token)

    assert driver.calls == [("key", name) for name in HARDWARE_KEYS]


def test_out_of_range_intents_are_rejected_before_the_device(tmp_path: Path):
    manager, driver = _manager(tmp_path)
    session = manager.start("vphone-local")

    for payload in (
        {"event_id": "a", "kind": "tap", "px": 430, "py": 10, "frame_seq": 1},
        {"event_id": "b", "kind": "tap", "px": 10, "py": 932, "frame_seq": 1},
        {"event_id": "c", "kind": "type", "text": "", "frame_seq": 1},
        {"event_id": "d", "kind": "key", "frame_seq": 1},          # key 缺 name
        {"event_id": "e", "kind": "key", "name": "back", "frame_seq": 1},  # iOS 没有 back 键
        {"event_id": "f", "kind": "pinch", "frame_seq": 1},
        {"kind": "tap", "px": 1, "py": 1, "frame_seq": 1},
    ):
        with pytest.raises(DeviceConsoleError) as exc:
            manager.event(session["session_id"], payload, session["controller_token"])
        assert exc.value.status == 422

    assert driver.calls == []


def test_web_recording_and_console_cannot_hold_the_same_device(tmp_path: Path):
    from autophone.record.web import WebRecordError, WebRecordManager
    from autophone.observation.record_capture import RecordSession, _fake_transport

    lock = ScreenLock(root=tmp_path / "runs/.locks")
    console = DeviceConsoleManager(lock=lock, driver_factory=FakeDriver, clock=lambda: 1000.0)
    transport = _fake_transport()
    recorder = WebRecordManager(
        root=tmp_path,
        lock=lock,
        session_factory=lambda *a, **kw: RecordSession(*a, transport=transport, **kw),
        clock=lambda: 1000.0,
        background_start=False,
    )
    session = console.start("vphone-local")

    with pytest.raises(WebRecordError) as exc:
        recorder.start("demo", build="b", starts_with=None, device="vphone-local")
    assert exc.value.code == "device_busy" and "web_control" in str(exc.value)

    console.stop(session["session_id"], session["controller_token"])
    started = recorder.start("demo", build="b", starts_with=None, device="vphone-local")
    assert started["state"] == "live"

    # ...and the other way round: 录制中不能再开远程操作会话。
    with pytest.raises(DeviceConsoleError) as console_exc:
        console.start("vphone-local")
    assert console_exc.value.code == "device_busy"
    recorder.shutdown()


def test_console_api_adapters_map_errors_and_carry_the_controller_token(monkeypatch):
    calls: list[tuple] = []
    manager = SimpleNamespace(
        device_status=lambda device: {"device": device, "active": False},
        start=lambda device, request_id=None: calls.append(("start", device, request_id))
        or {"session_id": "ctl-1", "device": device, "state": "live"},
        view=lambda sid, device=None: calls.append(("view", device)) or {"session_id": sid, "state": "live"},
        frame=lambda sid, device=None: calls.append(("frame", device)) or (b"jpeg", 4, 12.5),
        event=lambda sid, body, token, device=None: calls.append(("event", device, body, token)) or {"ok": True},
        heartbeat=lambda sid, token, device=None: calls.append(("heartbeat", device, token)) or {"ok": True},
        stop=lambda sid, token, device=None, reason="stopped": calls.append(("stop", device, token)) or {"reason": reason},
    )
    # BL-100 之后路由不再直接调 `api.device_console`：它经交互通道的操作面
    # （兼容路径下就是 `local_device_sessions`，控制面模式下是 rig 进程里的同一个类）。
    # 桩挂在操作面的 console 上，测的仍是「路径里的设备与 token 有没有透传到会话管理器」。
    monkeypatch.setattr(api.local_device_sessions, "console", manager)
    request = api.Request({"type": "http", "headers": [(b"x-controller-token", b"tok")]})

    assert api.device_console_status("rig")["active"] is False
    assert api.device_console_start("rig")["session_id"] == "ctl-1"
    assert api.device_console_view("rig", "ctl-1")["state"] == "live"
    response = api.device_console_frame("rig", "ctl-1")
    assert response.body == b"jpeg" and response.headers["x-frame-seq"] == "4"
    assert api.device_console_event("rig", "ctl-1", request, {"kind": "tap"})["ok"]
    assert api.device_console_heartbeat("rig", "ctl-1", request)["ok"]
    assert api.device_console_stop("rig", "ctl-1", request)["reason"] == "stopped"
    # 无 request_id 的创建由 web 侧生成一个（BL-107 §8.2 的兼容行为）：会话在日志与
    # 预留 owner 里可追溯，但每次调用的 ID 不同，所以没有跨调用幂等。
    generated = [call[2] for call in calls if call[0] == "start"]
    assert generated and generated[0] and generated[0].startswith("web-")
    # 调用方自带的稳定 ID 原样透传，不被覆盖。
    assert api.device_console_start("rig", {"request_id": "req-stable"})["session_id"] == "ctl-1"
    assert [call[2] for call in calls if call[0] == "start"][-1] == "req-stable"
    # 路径里的设备必须透传给 manager（会话寻址的第二把钥匙），token 走 header。
    assert all(call[1] == "rig" for call in calls)
    assert [c[-1] for c in calls if c[0] in {"event", "heartbeat", "stop"}] == ["tok"] * 3

    def busy(device, request_id=None):
        raise DeviceConsoleError("device is busy: run R1", code="device_busy")

    monkeypatch.setattr(manager, "start", busy)
    with pytest.raises(api.HTTPException) as exc:
        api.device_console_start("rig")
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "device_busy"


def test_console_routes_require_auth_when_credentials_are_configured(monkeypatch):
    monkeypatch.setattr(api, "_BASIC_AUTH_USERS", {"u": "p"})

    async def call(path: str, authorization: str | None = None) -> int:
        headers = []
        if authorization:
            headers.append((b"authorization", authorization.encode()))
        request = api.Request({
            "type": "http", "method": "GET", "path": path, "headers": headers, "query_string": b"",
            "scheme": "http", "server": ("test", 80), "client": ("10.0.0.5", 1),
        })

        async def next_handler(_request):
            return api.PlainTextResponse("ok")

        return (await api._basic_auth(request, next_handler)).status_code

    good = "Basic " + base64.b64encode(b"u:p").decode()
    for path in ("/api/devices/rig/console", "/api/devices/rig/console/ctl-1/frame"):
        assert asyncio.run(call(path)) == 401
        assert asyncio.run(call(path, good)) == 200


def test_shutdown_closes_sessions_and_frees_the_device(tmp_path: Path):
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(lock=lock, driver_factory=FakeDriver, clock=lambda: 1000.0)
    session = manager.start("vphone-local")

    manager.shutdown()

    assert lock.status(device="vphone-local").get("locked") is False
    with pytest.raises(DeviceConsoleError) as exc:
        manager.view(session["session_id"])
    assert exc.value.code == "session_closed"


# ── BL-107 创建与释放故障契约（docs/platform-device-console-embed.md §八） ──────


class FakeReservations:
    """控制面 `rig_reservations` 的替身：记下每次登记与释放，可按需失败。"""

    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.created: list[dict] = []
        self.released: list[tuple[str, str]] = []
        self.create_error: Exception | None = None
        self.release_error: Exception | None = None
        self._seq = 0

    def create(self, *, device: str, owner: dict) -> str | None:
        if self.create_error is not None:
            raise self.create_error
        if not self.configured:
            return None
        self._seq += 1
        reservation_id = f"RSV-{self._seq}"
        self.created.append({"reservation_id": reservation_id, "device": device, "owner": owner})
        return reservation_id

    def release(self, reservation_id: str, *, actor: str) -> None:
        if self.release_error is not None:
            raise self.release_error
        self.released.append((reservation_id, actor))


def _console(
    tmp_path: Path,
    *,
    driver: FakeDriver | None = None,
    reservations=None,
    clock=None,
    release_cleanup=None,
    stop_gate_wait_s: float = 20.0,
) -> tuple[DeviceConsoleManager, FakeDriver, ScreenLock]:
    drv = driver or FakeDriver()
    lock = ScreenLock(root=tmp_path / "runs/.locks")
    manager = DeviceConsoleManager(
        lock=lock,
        driver_factory=lambda: drv,
        clock=clock or (lambda: 1000.0),
        reservations=reservations if reservations is not None else FakeReservations(),
        release_cleanup=release_cleanup,
        stop_gate_wait_s=stop_gate_wait_s,
    )
    return manager, drv, lock


def test_a_lost_start_response_is_retried_onto_the_same_session(tmp_path: Path):
    """§8.7 场景一：启动成功但响应丢失。

    调用方没收到回包就用同一个 `request_id` 重试，必须拿回原 session，且不第二次占锁、
    不第二次登记预留——否则一个用户的一次请求会占住两台设备。
    """
    reservations = FakeReservations()
    manager, _, lock = _console(tmp_path, reservations=reservations)

    first = manager.start("vphone-local", request_id="req-1")
    retry = manager.start("vphone-local", request_id="req-1")

    assert retry["session_id"] == first["session_id"]
    assert retry["controller_token"] == first["controller_token"]
    assert retry["lifecycle"] == "active"
    # 锁只被占了一次，持有者就是那条会话。
    assert lock.status(device="vphone-local")["owner_id"] == first["session_id"]
    assert len(reservations.created) == 1
    # 换一个 request_id 才是另一次申请——它被 ScreenLock 挡住，不会开出第二条会话。
    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local", request_id="req-2")
    assert exc.value.code == "device_busy"


def test_a_retry_after_the_session_ended_does_not_allocate_a_second_device(tmp_path: Path):
    """同一 `request_id` 在原会话结束后重试：拿到 410，不另分配一台（§8.2）。"""
    manager, _, _ = _console(tmp_path)
    session = manager.start("vphone-local", request_id="req-1")
    manager.stop(session["session_id"], session["controller_token"])

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local", request_id="req-1")
    assert exc.value.status == 410 and exc.value.code == "session_closed"


def test_concurrent_requests_with_one_id_share_one_session_and_one_lease(tmp_path: Path):
    """§8.7 场景四：同用户并发申请。同一 `request_id` 并发只占一次锁。"""
    reservations = FakeReservations()
    manager, _, lock = _console(tmp_path, reservations=reservations)
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def request() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(manager.start("vphone-local", request_id="req-1"))
        except Exception as exc:  # noqa: BLE001 - 并发下的任何异常都要看得见
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert len(results) == 4
    assert len({item["session_id"] for item in results}) == 1
    assert len(reservations.created) == 1
    assert lock.status(device="vphone-local")["owner_id"] == results[0]["session_id"]


def test_a_known_start_failure_is_not_cached_against_the_request_id(tmp_path: Path):
    """明确失败（确定没占到设备）不缓存结论，同一 ID 重试仍可成功。"""
    manager, _, lock = _console(tmp_path)
    lease = lock.try_lease(device="vphone-local", owner_kind="run", owner_id="R0007")

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local", request_id="req-1")
    assert exc.value.code == "device_busy"

    lease.release()
    assert manager.start("vphone-local", request_id="req-1")["lifecycle"] == "active"


def test_start_without_a_request_id_keeps_the_previous_behaviour(tmp_path: Path):
    """不带 `request_id`：没有跨调用幂等，第二次照旧被 ScreenLock 挡住。"""
    manager, _, _ = _console(tmp_path)
    manager.start("vphone-local")

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local")
    assert exc.value.code == "device_busy"


def test_console_start_registers_and_releases_a_control_plane_reservation(tmp_path: Path):
    """`console_start` 成功即登记预留，关闭即释放（设计文档 §十三）。"""
    reservations = FakeReservations()
    manager, _, _ = _console(tmp_path, reservations=reservations)

    session = manager.start("vphone-local", request_id="req-1")

    assert session["reservation_id"] == "RSV-1"
    assert reservations.created[0]["device"] == "vphone-local"
    owner = reservations.created[0]["owner"]
    assert owner["kind"] == "device_console"
    assert owner["grant_id"] == "req-1" and owner["session_id"] == session["session_id"]

    closed = manager.stop(session["session_id"], session["controller_token"])
    assert closed["closed_reason"] == "stopped" and closed["error_reason"] is None
    assert reservations.released == [("RSV-1", "device_console:stopped")]


def test_without_a_control_plane_no_reservation_is_registered_and_nothing_fails(tmp_path: Path):
    """兼容路径（控制面未配置）：不登记、不报错、关闭时也不去释放。"""
    reservations = FakeReservations(configured=False)
    manager, _, _ = _console(tmp_path, reservations=reservations)

    session = manager.start("vphone-local", request_id="req-1")
    assert session["reservation_id"] is None

    manager.stop(session["session_id"], session["controller_token"])
    assert reservations.created == [] and reservations.released == []


def test_a_failed_reservation_registration_rolls_the_session_back(tmp_path: Path):
    """§8.7 场景二之一：控制面重启导致登记失败。

    补偿选「回滚 rig 侧会话」：本次会话一个动作都没派发过，还锁不会打断任何在飞动作，
    设备立刻干净可用。登记结果未知这件事进对账清单。
    """
    reservations = FakeReservations()
    reservations.create_error = RuntimeError("control plane is configured but unavailable")
    manager, _, lock = _console(tmp_path, reservations=reservations)

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local", request_id="req-1")

    assert exc.value.status == 503 and exc.value.code == "reservation_failed"
    assert lock.status(device="vphone-local").get("locked") is False
    pending = manager.pending_reconciliation()
    assert [entry["kind"] for entry in pending] == ["reservation_register_unknown"]
    assert pending[0]["request_id"] == "req-1"
    # 设备没有被隔离：它确实是干净的，隔离一台健康设备是纯损失。
    reservations.create_error = None
    assert manager.start("vphone-local", request_id="req-2")["lifecycle"] == "active"


def test_a_failed_reservation_release_is_recorded_and_retriable(tmp_path: Path):
    """§8.7 场景二之二：关闭时控制面不在，释放失败进对账并可重试。"""
    reservations = FakeReservations()
    manager, _, lock = _console(tmp_path, reservations=reservations)
    session = manager.start("vphone-local", request_id="req-1")
    reservations.release_error = RuntimeError("control plane is unavailable")

    closed = manager.stop(session["session_id"], session["controller_token"])

    # 用户这一侧照常结束，锁也还了：还不掉的是控制面那一行，不是设备。
    assert closed["closed_reason"] == "stopped" and closed["error_reason"] is None
    assert lock.status(device="vphone-local").get("locked") is False
    assert [entry["kind"] for entry in manager.pending_reconciliation()] == [
        "reservation_release_failed",
    ]

    reservations.release_error = None
    assert manager.retry_reconciliation() == {"released": 1, "pending": 0}
    assert reservations.released == [("RSV-1", "device_console:reconcile")]
    assert manager.pending_reconciliation() == []


def test_close_all_waits_for_the_inflight_action_before_handing_the_device_back(tmp_path: Path):
    """§8.7 场景三：动作在飞时 rig 掉线。

    `close_all` 原来明写「不等在飞动作」就还锁。对端在不在与设备上有没有一次动作在飞
    是两件事：那次 UDS 调用照样会落到设备上（§8.3）。
    """
    driver = FakeDriver()
    driver.before_action = threading.Event()
    driver.release_action = threading.Event()
    manager, _, lock = _console(tmp_path, driver=driver)
    session = manager.start("vphone-local", request_id="req-1")

    slow = threading.Thread(target=lambda: _tap(manager, session, event_id="e1", seq=1))
    slow.start()
    assert driver.before_action.wait(timeout=5)
    closer = threading.Thread(target=lambda: manager.close_all("rig_disconnected"))
    closer.start()
    try:
        time.sleep(0.05)
        assert lock.status(device="vphone-local").get("locked") is True
    finally:
        driver.release_action.set()
        slow.join(timeout=5)
        closer.join(timeout=5)

    assert driver.calls == [("tap", 100, 200)]
    assert lock.status(device="vphone-local").get("locked") is False


def test_an_event_submitted_after_closing_starts_is_refused(tmp_path: Path):
    """进入 `closing` 即拒绝新动作（§8.3）。"""
    driver = FakeDriver()
    driver.before_action = threading.Event()
    driver.release_action = threading.Event()
    manager, _, _ = _console(tmp_path, driver=driver)
    session = manager.start("vphone-local", request_id="req-1")

    slow = threading.Thread(target=lambda: _tap(manager, session, event_id="e1", seq=1))
    slow.start()
    assert driver.before_action.wait(timeout=5)
    closer = threading.Thread(
        target=lambda: manager.stop(session["session_id"], session["controller_token"]),
    )
    closer.start()
    try:
        # 会话已在 closing（stop 正等这次在飞动作落地），此时提交的新动作必须被拒。
        deadline = time.monotonic() + 5
        while manager.view(session["session_id"])["lifecycle"] != "closing":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(DeviceConsoleError) as exc:
            _tap(manager, session, event_id="e2", px=300, py=300, seq=1)
        assert exc.value.status == 409 and exc.value.code == "session_closing"
    finally:
        driver.release_action.set()
        slow.join(timeout=5)
        closer.join(timeout=5)

    assert driver.calls == [("tap", 100, 200)]


def test_an_action_that_took_its_reference_before_the_close_does_not_reach_the_device(
    tmp_path: Path,
):
    """取到会话引用之后才被关闭的动作不得落到设备上（§8.3 的那个窗口）。

    原实现里 `event()` 先取引用再抢 gate，`stop()` 在这两步之间完成并还锁后，那一下
    动作照样驱动设备。这里把线程停在两步之间，验证 gate 内的第二道存活检查拦住了它。
    """
    manager, driver, lock = _console(tmp_path)
    session = manager.start("vphone-local", request_id="req-1")
    original = manager._reject_unless_active
    reached_window = threading.Event()
    closed = threading.Event()
    checks: list[int] = []

    def paused_check(sess):
        original(sess)
        checks.append(1)
        if len(checks) == 1:
            # 引用已取到、gate 还没抢：让 stop 在这里整条跑完。
            reached_window.set()
            assert closed.wait(timeout=5)

    manager._reject_unless_active = paused_check
    errors: list[Exception] = []

    def act() -> None:
        try:
            _tap(manager, session, event_id="e1", seq=1)
        except Exception as exc:  # noqa: BLE001 - 断言要看到具体拒绝
            errors.append(exc)

    actor = threading.Thread(target=act)
    actor.start()
    assert reached_window.wait(timeout=5)
    manager.stop(session["session_id"], session["controller_token"])
    closed.set()
    actor.join(timeout=5)

    assert len(errors) == 1 and isinstance(errors[0], DeviceConsoleError)
    assert errors[0].code == "session_closing"
    assert driver.calls == []
    assert lock.status(device="vphone-local").get("locked") is False


def test_a_cleanup_failure_keeps_the_device_unallocatable(tmp_path: Path):
    """§8.4：清理失败记 `error_reason`，设备进入不可分配状态，不宣告可复用。"""
    reservations = FakeReservations()

    def failing_cleanup(device: str, session_id: str) -> None:
        raise RuntimeError("reset to home did not confirm")

    manager, _, lock = _console(
        tmp_path, reservations=reservations, release_cleanup=failing_cleanup,
    )
    session = manager.start("vphone-local", request_id="req-1")

    closed = manager.stop(session["session_id"], session["controller_token"])

    assert closed["lifecycle"] == "closed" and closed["closed_reason"] == "stopped"
    assert "reset to home did not confirm" in closed["error_reason"]
    # 锁没还、预留没释放：两道互斥一起留着，run 与 Scheduler 都到不了这台设备。
    assert lock.status(device="vphone-local").get("locked") is True
    assert reservations.released == []
    assert manager.unavailable_devices()[0]["device"] == "vphone-local"

    with pytest.raises(DeviceConsoleError) as exc:
        manager.start("vphone-local", request_id="req-2")
    assert exc.value.code == "device_unavailable"

    # 恢复验证通过后才放回：锁与预留同时撤掉。
    manager.clear_unavailable("vphone-local", actor="tester")
    assert lock.status(device="vphone-local").get("locked") is False
    assert reservations.released == [("RSV-1", "device_console:recovered:tester")]
    assert manager.start("vphone-local", request_id="req-3")["lifecycle"] == "active"


def test_an_inflight_action_that_never_settles_marks_the_device_unavailable(tmp_path: Path):
    """在飞动作等不到落地也是清理失败：结束用户访问，但设备不可分配（§8.4 / §8.5）。"""
    driver = FakeDriver()
    driver.before_action = threading.Event()
    driver.release_action = threading.Event()
    manager, _, lock = _console(tmp_path, driver=driver, stop_gate_wait_s=0.05)
    session = manager.start("vphone-local", request_id="req-1")

    slow = threading.Thread(target=lambda: _tap(manager, session, event_id="e1", seq=1))
    slow.start()
    assert driver.before_action.wait(timeout=5)
    try:
        closed = manager.stop(session["session_id"], session["controller_token"])
        assert "inflight_action_not_settled" in closed["error_reason"]
        assert lock.status(device="vphone-local").get("locked") is True
        with pytest.raises(DeviceConsoleError) as exc:
            manager.start("vphone-local", request_id="req-2")
        assert exc.value.code == "device_unavailable"
    finally:
        driver.release_action.set()
        slow.join(timeout=5)


def test_expiry_and_revocation_share_the_close_path(tmp_path: Path):
    """到期与撤销（§7.2 第 5 条）复用同一条关闭路径，只是 `reason` 不同。"""
    reservations = FakeReservations()
    manager, _, lock = _console(tmp_path, reservations=reservations)
    session = manager.start("vphone-local", request_id="req-1")

    closed = manager.stop(
        session["session_id"], session["controller_token"], reason="grant_revoked",
    )

    assert closed["closed_reason"] == "grant_revoked" and closed["reason"] == "grant_revoked"
    assert reservations.released == [("RSV-1", "device_console:grant_revoked")]
    assert lock.status(device="vphone-local").get("locked") is False


def test_the_channel_carries_request_id_so_a_repeat_start_takes_no_second_lease(tmp_path: Path):
    """判据原文：同一 `request_id` 重复 `console_start` 不二次占锁——经交互通道验一遍。"""
    from autophone.application.device_session import DeviceSessionOps

    reservations = FakeReservations()
    manager, _, lock = _console(tmp_path, reservations=reservations)
    ops = DeviceSessionOps(
        console=manager,
        screenshot=lambda: PREVIEW_JPEG,
        status=lambda device: {"device": device},
        target=lambda: {"device": "vphone-local"},
    )

    first = ops.call("vphone-local", "console_start", {"request_id": "req-1"})
    again = ops.call("vphone-local", "console_start", {"request_id": "req-1"})

    assert again["session_id"] == first["session_id"]
    assert len(reservations.created) == 1
    assert lock.status(device="vphone-local")["owner_id"] == first["session_id"]

    ops.call("vphone-local", "console_stop", {
        "session_id": first["session_id"], "controller_token": first["controller_token"],
        "reason": "grant_expired",
    })
    assert reservations.released == [("RSV-1", "device_console:grant_expired")]


def test_every_uds_action_skips_the_device_side_screenshot(monkeypatch):
    """判据：console 的每个动作都带 `screen: False`。

    动作返回的截图这里没有调用者读——`event()` 成功后还会把 `frame_data` 置空。
    而设备侧 `screen:true` 的一次动作约 540ms、`screen:false` 约 1ms（BL-101 活机
    实测）。少一个 `screen: False` 就是每次点击白付一张随后被丢弃的截图。
    """
    from autophone.device import uds as uds_mod
    from autophone.application.device_console import UdsDeviceDriver

    sent: list[dict] = []

    def fake_request(msg, *, sock_path, timeout, **kwargs):
        sent.append(dict(msg))
        return {"ok": True}, SimpleNamespace(image_w=430, image_h=932)

    monkeypatch.setattr(uds_mod, "request", fake_request)
    driver = UdsDeviceDriver(sock_path="/tmp/no-such.sock")

    driver.tap(100, 200)
    driver.swipe(10, 20, 30, 40, ms=120)
    driver.type_text("hello")
    for key in HARDWARE_KEYS:
        driver.press_key(key)

    assert [m["t"] for m in sent] == ["tap", "swipe", "type"] + ["key"] * len(HARDWARE_KEYS)
    assert all(m.get("screen") is False for m in sent), sent


def test_the_frame_path_still_asks_for_a_picture(monkeypatch):
    """反面判据：`screenshot()` 用得到返回的图，不能被顺手一起关掉。"""
    from autophone.device import uds as uds_mod
    from autophone.application.device_console import UdsDeviceDriver

    sent: list[dict] = []

    def fake_request(msg, *, sock_path, timeout, **kwargs):
        sent.append(dict(msg))
        return (
            {"ok": True, "image": base64.b64encode(PREVIEW_JPEG).decode()},
            SimpleNamespace(image_w=430, image_h=932),
        )

    monkeypatch.setattr(uds_mod, "request", fake_request)
    driver = UdsDeviceDriver(sock_path="/tmp/no-such.sock")

    assert driver.screenshot() == (PREVIEW_JPEG, 430, 932)
    assert sent[0]["t"] == "screenshot"
    assert sent[0].get("screen") is not False


def test_concurrent_frames_share_capture_even_when_capture_exceeds_interval(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    now = [1000.0]
    manager, driver = _manager(tmp_path, clock=lambda: now[0])
    created = manager.start('rig')
    entered, release = threading.Event(), threading.Event()
    attempts = threading.Barrier(9)
    calls = []

    def capture():
        calls.append('capture')
        entered.set()
        assert release.wait(3)
        now[0] = 1002.0
        return b'new-frame', 430, 932

    driver.screenshot = capture
    now[0] = 1001.0

    def fetch():
        attempts.wait(timeout=3)
        return manager.frame(created['session_id'])

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch) for _ in range(8)]
            attempts.wait(timeout=3)
            assert entered.wait(3)
            # Keep the device call in flight while the concurrent requests arrive.
            time.sleep(0.05)
            release.set()
            frames = [f.result(timeout=3) for f in futures]
        assert calls == ['capture']
        assert frames == [(b'new-frame', 2, 1002.0)] * 8
        now[0] += 0.121
        assert manager.frame(created['session_id'])[1] == 3
        assert len(calls) == 2
    finally:
        release.set()
        manager.close_all('test_done')


def test_failed_frame_refresh_does_not_publish_or_return_expired_cache(tmp_path):
    now = [1000.0]
    manager, driver = _manager(tmp_path, clock=lambda: now[0])
    created = manager.start('rig')
    now[0] += 1
    driver.screenshot = lambda: (_ for _ in ()).throw(OSError('capture failed'))
    try:
        with pytest.raises(DeviceConsoleError, match='capture failed'):
            manager.frame(created['session_id'])
        assert manager.view(created['session_id'])['seq'] == 1
        driver.screenshot = lambda: (b'recovered', 430, 932)
        assert manager.frame(created['session_id']) == (b'recovered', 2, now[0])
    finally:
        manager.close_all('test_done')


def test_action_invalidation_cannot_be_overwritten_by_inflight_frame(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    now = [1000.0]
    manager, driver = _manager(tmp_path, clock=lambda: now[0])
    created = manager.start('rig')
    driver.before_action, driver.release_action = threading.Event(), threading.Event()
    entered, release = threading.Event(), threading.Event()

    def capture():
        entered.set()
        assert release.wait(3)
        return b'concurrent-frame', 430, 932

    driver.screenshot = capture
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            action = pool.submit(_tap, manager, created, event_id='during-frame')
            assert driver.before_action.wait(3)
            now[0] += 1
            frame = pool.submit(manager.frame, created['session_id'])
            assert entered.wait(3)
            driver.release_action.set()
            time.sleep(0.05)
            release.set()
            assert frame.result(timeout=3)[1] == 2
            assert action.result(timeout=3)['ok']
        driver.screenshot = lambda: (b'after-action', 430, 932)
        assert manager.frame(created['session_id']) == (b'after-action', 3, now[0])
    finally:
        driver.release_action.set()
        release.set()
        manager.close_all('test_done')
