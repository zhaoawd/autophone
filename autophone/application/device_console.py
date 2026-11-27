"""Device console: drive the VM straight from the browser, recording nothing.

The web *recording* surface (``record_web``) exists to freeze人手操作 into a
trace asset.  A lot of device work is not that: 看一眼当前页面、把 app 点回某个
状态、手工验一个猜想。Doing those through a recording means a throwaway
``runs/rec`` directory每次都要 cancel，还占着「录制中」这个语义。

This module is the second browser surface: the same dumb-display + intent-entry
seam, the same ScreenLock mutex, the same one-action-in-flight and event_id
idempotency guarantees — **minus every artifact**.  Nothing is written to disk,
no trace, no shots, no recording row.  A console session is pure device time.

不变量（与 record_web 对齐，不得回退）：
- 一个 event_id 最多驱动一次设备。重复提交拿回同一个 ack（含失败 ack），
  从不二次派发。
- 一个会话同时只有一个在飞动作（非阻塞门，撞上即 409）。
- 帧过期即拒绝动作：人看着 10 秒前的画面点下去，落点已经不可信。
- 设备互斥走 ScreenLock 真 lease（``owner_kind="web_control"``），run 与录制
  看得见它、且不排队等它；结束会话前先等在飞动作落地，lease 不会在一个动作
  半空中被还回去（否则那一下 tap 会落在下一个 run 身上）。
- 进程重启 = 会话消失（内存态）。前端只能重开一个新会话，绝不把旧 event_id
  重放进新会话——没有产物要保全，重放只会多一次设备动作。

创建与释放的故障契约（BL-107，docs/platform-device-console-embed.md §八）：
- 会话生命周期是四态 ``creating / active / closing / closed``，另记
  ``closed_reason`` 与 ``error_reason``。一期没有 ``access_grants`` 表（BL-104），
  状态机由本模块的会话对象承载。
- 创建按 ``request_id`` 幂等：同一 ID 并发或重试返回原 session，不二次占锁。
- 全部关闭路径（``stop``、心跳超时、``close_all``、到期与撤销）走同一个
  ``_close``：先进 ``closing`` 拒绝新动作，再等在飞动作落地，再做清理，最后还锁。
  取到会话引用之后才被关闭的动作在 gate 内被拒，不落到设备上。
- 清理失败（在飞动作未落地、复位钩子抛错）记 ``error_reason``，设备进入不可分配
  状态且**不还锁**，等 ``clear_unavailable()`` 做恢复验证后才重新可用。
- ``console_start`` 成功后在控制面登记一条 ``rig_reservations``，关闭时释放；
  控制面未配置时不登记、不报错。登记失败回滚本次会话，释放失败进对账清单。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from autophone.device.lock import ScreenLease, ScreenLock
from autophone.record.web import normalize_preview_intent
from autophone.util import now_iso
from autophone.device.uds import EXPECTED_PREVIEW, preview_to_native

logger = logging.getLogger(__name__)

FRAME_MAX_AGE_S = 10.0
FRAME_MIN_INTERVAL_S = 0.12
HEARTBEAT_TTL_S = 90.0
HEARTBEAT_SCAN_S = 15.0
# 关会话前等在飞动作落地的上限。必须大于驱动自己的超时（UDS 15s），否则
# 「等不到就还锁」会退化成 stop 期间照样丢锁。
STOP_GATE_WAIT_S = 20.0
ACK_HISTORY = 256
CLOSED_HISTORY = 32
# 同一个 request_id 的第二个调用者等第一个出结论的上限。比 STOP_GATE_WAIT_S 短：
# 它等的是一次 start（截图 + 登记预留），不是一个在飞的设备动作。
START_WAIT_S = 30.0
# 创建幂等表的上限。淘汰一条只意味着更久之前的那个 request_id 重试时会当新请求处理，
# 不影响任何已在跑的会话。
REQUEST_HISTORY = 256

# 会话生命周期四态（设计文档 §8.1）。
LIFECYCLE_CREATING = "creating"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_CLOSING = "closing"
LIFECYCLE_CLOSED = "closed"


class DeviceConsoleError(RuntimeError):
    def __init__(self, message: str, *, status: int = 409, code: str = "conflict") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _intent_hash(intent: dict[str, Any]) -> str:
    raw = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DeviceUnavailable(RuntimeError):
    """设备侧拒绝或不可达——动作没有落在设备上。"""


class ReservationPort:
    """`rig_reservations` 登记面的接口形状。

    两个方法都由 `DeviceConsoleManager` 在会话边界调用，实现可以是控制面适配器
    （下面的 `ControlPlaneReservations`）或测试替身。
    """

    def create(self, *, device: str, owner: dict[str, Any]) -> str | None:
        """登记一条预留，返回 reservation_id；控制面未配置时返回 None。"""
        raise NotImplementedError

    def release(self, reservation_id: str, *, actor: str) -> None:
        """释放一条预留。失败要抛异常——调用方据此进对账清单。"""
        raise NotImplementedError


class ControlPlaneReservations:
    """把 console 会话登记成控制面的一条 `rig_reservations`（设计文档 §十三）。

    理由：调度资格检查（`autophone/control/postgres.py`）已经消费预留并在不匹配时返回
    `reservation_mismatch`，但它只看目标 rig、能力集合与预留，没有任何按接入方配置排除
    设备的逻辑。不登记预留，Scheduler 仍会把 Assignment 派给正被人操作的 rig，要等到
    rig 侧 ScreenLock 才被拒。

    设备键即 rig 标识：rig 目标把 `rig_id` 同时写成 `AP_DEVICE`
    （`autophone/rig/target.py`），所以这里直接用 device 作 `rig_id`。

    控制面未配置（`AUTOPHONE_DATABASE_URL` 未设）时 `create` 返回 None——兼容路径不登记、
    不报错。已配置但连不上会由 `configured_control_plane()` 抛异常，按 BL-026 不落回
    兼容路径，由调用方处理。
    """

    def __init__(self, control_factory: Callable[[], Any] | None = None) -> None:
        self._control_factory = control_factory

    def _control(self) -> Any:
        factory = self._control_factory
        if factory is None:
            # 压进函数体：`autophone.control` 硬依赖 psycopg，而 psycopg 只在 platform
            # 依赖组里；没装该组时 import 本模块仍要成立。
            from autophone.application.control_plane import configured_control_plane

            factory = configured_control_plane
        return factory()

    def create(self, *, device: str, owner: dict[str, Any]) -> str | None:
        control = self._control()
        if control is None:
            return None
        row = control.create_reservation({"rig_id": device, "owner": dict(owner)})
        reservation_id = str((row or {}).get("reservation_id") or "").strip()
        return reservation_id or None

    def release(self, reservation_id: str, *, actor: str) -> None:
        control = self._control()
        if control is None:
            # 登记时控制面还在、释放时读不到配置：这条预留留在库里，交给对账，
            # 不当成「已释放」。
            raise RuntimeError(
                f"control plane is not configured; reservation {reservation_id} not released"
            )
        control.release_reservation(reservation_id, actor)


class UdsDeviceDriver:
    """Live UDS driver.  Injected as a seam so the manager is testable无设备。"""

    def __init__(self, sock_path: str | None = None, *, timeout: float = 15.0) -> None:
        self._sock_path = sock_path
        self._timeout = timeout

    def _sock(self) -> str:
        if self._sock_path:
            return self._sock_path
        from autophone.execution.engine.actions import resolve_sock

        self._sock_path = resolve_sock()
        return self._sock_path

    def _request(self, msg: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        from autophone.device.uds import request

        try:
            resp, sample = request(msg, sock_path=self._sock(), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — transport failure is a device failure
            raise DeviceUnavailable(f"{msg.get('t')}: {exc}") from exc
        if not resp.get("ok"):
            raise DeviceUnavailable(f"{msg.get('t')} not ok: {resp.get('error')!r}")
        return resp, sample

    def screenshot(self) -> tuple[bytes, int, int]:
        resp, sample = self._request({"t": "screenshot", "color": True})
        image = resp.get("image")
        if not image:
            raise DeviceUnavailable("screenshot response has no image")
        if sample.image_w is None or sample.image_h is None:
            raise DeviceUnavailable("could not decode screenshot dimensions")
        return base64.b64decode(image), int(sample.image_w), int(sample.image_h)

    # 全部动作一律 `screen: False`：动作的返回截图这里没有一个调用者读——`event()`
    # 成功后还会把 `frame_data` 置空、让下一次 `frame()` 重新取图。设备侧带截图的
    # 一次动作约 540ms，不带约 1ms（BL-101 活机实测），该截图没有调用者读取。要画面
    # 只走 `screenshot()`。
    def tap(self, px: int, py: int) -> None:
        nx, ny = preview_to_native(px, py)
        self._request({"t": "tap", "x": nx, "y": ny, "screen": False})

    def swipe(self, px1: int, py1: int, px2: int, py2: int, *, ms: int) -> None:
        nx1, ny1 = preview_to_native(px1, py1)
        nx2, ny2 = preview_to_native(px2, py2)
        self._request(
            {"t": "swipe", "x1": nx1, "y1": ny1, "x2": nx2, "y2": ny2, "ms": ms, "screen": False}
        )

    def type_text(self, text: str) -> None:
        # ``type`` 是剪贴板写入（CLAUDE.md）。
        self._request({"t": "type", "text": text, "screen": False})

    def press_key(self, name: str) -> None:
        self._request({"t": "key", "name": name, "screen": False})


@dataclass
class _Session:
    session_id: str
    device: str
    token_hash: str
    lease: ScreenLease
    driver: Any
    geometry: dict[str, Any]
    started_at: str
    heartbeat_at: float
    gate: threading.Lock = field(default_factory=threading.Lock)
    frame_gate: threading.Lock = field(default_factory=threading.Lock)
    frame_seq: int = 0
    frame_data: bytes | None = None
    frame_at: float = 0.0
    frame_times: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=256))
    acks: "OrderedDict[str, dict[str, Any]]" = field(default_factory=OrderedDict)
    inflight: dict[str, Any] | None = None
    action_count: int = 0
    last_action_at: str | None = None
    # 故障契约（§八）：四态、创建请求标识、控制面预留、两个原因字段。
    lifecycle: str = LIFECYCLE_CREATING
    request_id: str | None = None
    reservation_id: str | None = None
    closed_reason: str | None = None
    error_reason: str | None = None


@dataclass
class _StartRequest:
    """一个 `request_id` 的创建结论，供并发调用者与重试共用。"""

    request_id: str
    done: threading.Event = field(default_factory=threading.Event)
    session_id: str | None = None
    result: dict[str, Any] | None = None
    error: BaseException | None = None


DriverFactory = Callable[[], Any]


class DeviceConsoleManager:
    """Deep module owning every live browser console session."""

    def __init__(
        self,
        *,
        lock: ScreenLock | None = None,
        driver_factory: DriverFactory = UdsDeviceDriver,
        clock: Callable[[], float] = time.time,
        heartbeat_ttl_s: float = HEARTBEAT_TTL_S,
        frame_max_age_s: float = FRAME_MAX_AGE_S,
        frame_min_interval_s: float = FRAME_MIN_INTERVAL_S,
        stop_gate_wait_s: float = STOP_GATE_WAIT_S,
        start_wait_s: float = START_WAIT_S,
        reservations: Any | None = None,
        release_cleanup: Callable[[str, str], None] | None = None,
    ) -> None:
        self.lock = lock or ScreenLock()
        self.driver_factory = driver_factory
        self.clock = clock
        self.heartbeat_ttl_s = heartbeat_ttl_s
        self.frame_max_age_s = frame_max_age_s
        self.frame_min_interval_s = frame_min_interval_s
        self.stop_gate_wait_s = stop_gate_wait_s
        self.start_wait_s = start_wait_s
        # 默认就是控制面适配器：未配置控制面时它自己 no-op，所以兼容路径行为不变。
        self.reservations = reservations if reservations is not None else ControlPlaneReservations()
        # 释放时的复位钩子（设计文档 §7.3）。一期不注入实现；抛错即「清理失败」，
        # 走 §8.4：记 error_reason、设备进不可分配、不还锁。
        self.release_cleanup = release_cleanup
        self._sessions: dict[str, _Session] = {}
        self._closed: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._requests: "OrderedDict[str, _StartRequest]" = OrderedDict()
        # 清理失败后不可分配的设备：device -> 记录（含被扣住的 lease）。
        self._unavailable: dict[str, dict[str, Any]] = {}
        # 预留登记 / 释放的失败补偿清单（设计文档 §十三「失败补偿纳入对账」）。
        self._reconciliation: list[dict[str, Any]] = []
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._scanner: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_scanner(self) -> None:
        if self._scanner and self._scanner.is_alive():
            return
        self._stop.clear()
        self._scanner = threading.Thread(
            target=self._scan_loop, name="device-console-heartbeat", daemon=True,
        )
        self._scanner.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._scanner:
            self._scanner.join(timeout=1.0)
        self.close_all("server_shutdown")

    def close_all(self, reason: str) -> int:
        """关掉全部会话，返回关掉的会话数。

        BL-100 的交互通道用它做「rig 掉线」的会话终止路径：连接一断，持设备的进程
        必须自己把 lease 还回去，否则没有别的进程能还它。

        **它与 stop 走同一条清理流程，会等在飞动作落地**（BL-107 / §8.3）。原实现
        不等就还锁，理由是「对端不在了」；但对端在不在与设备上有没有一次动作在飞是
        两件事：那一次 UDS 调用仍会落到设备上，锁提前还回去后，下一个 run 会在它落地
        之前拿到设备。等不到就按 §8.4 记 `error_reason` 并让设备进不可分配状态。
        """
        closed = 0
        for session in list(self._sessions.values()):
            self._close(session, reason=reason)
            closed += 1
        return closed

    def _scan_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SCAN_S):
            self.expire_heartbeats()

    # ── internals ────────────────────────────────────────────────────────────
    def _get(self, session_id: str, device: str | None = None) -> _Session:
        with self._guard:
            session = self._sessions.get(session_id)
            closed = self._closed.get(session_id)
        # 会话是按 session_id 寻址的，但它挂在 /api/devices/{device}/... 下面：
        # 路径里的设备与会话不符时报 404，别让 URL 撒谎（多设备时尤其要命）。
        if device is not None:
            if session is not None and session.device != device:
                session = None
                closed = None
            elif closed is not None and closed.get("device") != device:
                closed = None
        if session is not None:
            return session
        if closed is not None:
            raise DeviceConsoleError(
                f"console session已结束（{closed.get('reason')}）", status=410, code="session_closed",
            )
        raise DeviceConsoleError("console session not found", status=404, code="not_found")

    @staticmethod
    def _check_token(session: _Session, token: str | None) -> None:
        if not token or not secrets.compare_digest(session.token_hash, _token_hash(token)):
            raise DeviceConsoleError("controller token mismatch", code="token_mismatch")

    @contextmanager
    def _execution(self, session: _Session) -> Iterator[None]:
        if not session.gate.acquire(blocking=False):
            raise DeviceConsoleError("session action is in progress", code="busy")
        try:
            yield
        finally:
            session.gate.release()

    def _reject_unless_active(self, session: _Session) -> None:
        """进入 closing 即拒绝新动作（§8.3）。

        在 `event()` 里调用两次：取到会话引用后一次（快速拒绝），拿到 gate 之后再一次
        （取到引用与抢到 gate 之间会话可能已被关掉，那一下动作不得落到设备上）。
        """
        with self._guard:
            lifecycle = session.lifecycle
        if lifecycle == LIFECYCLE_ACTIVE:
            return
        if lifecycle == LIFECYCLE_CREATING:
            raise DeviceConsoleError(
                "console session is still being created", code="session_creating",
            )
        raise DeviceConsoleError(
            f"console session is {lifecycle}（{session.closed_reason or 'closing'}）",
            status=409, code="session_closing",
        )

    def _close(self, session: _Session, *, reason: str) -> dict[str, Any]:
        """唯一的清理流程：stop、心跳超时、close_all、到期与撤销全部走这里（§8.3）。

        顺序固定：进 `closing`（新动作即刻被拒）→ 等在飞动作落地 → 复位钩子 →
        释放控制面预留 → 还 lease。任一步失败按 §8.4 记 `error_reason`，设备进入
        不可分配状态且**不还锁**——清理失败可以结束用户访问，但不能据此宣告设备可复用。
        """
        with self._guard:
            if session.lifecycle in (LIFECYCLE_CLOSING, LIFECYCLE_CLOSED):
                # 另一条关闭路径已经在跑（如 stop 与心跳扫描撞上）：不重复清理。
                return self._closed.get(session.session_id) or {
                    "session_id": session.session_id,
                    "device": session.device,
                    "reason": session.closed_reason or reason,
                    "closed_reason": session.closed_reason or reason,
                    "error_reason": session.error_reason,
                    "lifecycle": session.lifecycle,
                    "action_count": session.action_count,
                }
            session.lifecycle = LIFECYCLE_CLOSING
            session.closed_reason = reason

        error_reason: str | None = None
        settled = session.gate.acquire(timeout=self.stop_gate_wait_s)
        if not settled:
            error_reason = f"inflight_action_not_settled_in_{self.stop_gate_wait_s}s"
        try:
            if error_reason is None and self.release_cleanup is not None:
                try:
                    self.release_cleanup(session.device, session.session_id)
                except Exception as exc:  # noqa: BLE001 - 清理失败要留在设备上
                    error_reason = f"release_cleanup_failed: {exc}"
        finally:
            if settled:
                session.gate.release()

        if error_reason is None:
            self._release_reservation(session, reason=reason)
        else:
            # 清理失败：预留一并留着。lease 挡住本机 run，预留挡住 Scheduler 派单，
            # 两道一起撤（`clear_unavailable`），不留「锁还在、单已派」的中间态。
            self._note_reconciliation({
                "kind": "reservation_retained_device_unavailable",
                "device": session.device,
                "session_id": session.session_id,
                "reservation_id": session.reservation_id,
                "error": error_reason,
            })

        with self._guard:
            session.lifecycle = LIFECYCLE_CLOSED
            session.error_reason = error_reason
            self._sessions.pop(session.session_id, None)
            record = {
                "session_id": session.session_id,
                "device": session.device,
                # `reason` 是既有线上字段名，`closed_reason` 是 §8.1 的字段名，同值并列。
                "reason": reason,
                "closed_reason": reason,
                "error_reason": error_reason,
                "lifecycle": LIFECYCLE_CLOSED,
                "request_id": session.request_id,
                "reservation_id": session.reservation_id,
                "action_count": session.action_count,
                "closed_at": now_iso(),
            }
            self._closed[session.session_id] = record
            while len(self._closed) > CLOSED_HISTORY:
                self._closed.popitem(last=False)
            if error_reason is not None:
                # 扣住 lease：ScreenLock 是设备互斥的真源，还了锁 run 就能拿到一台
                # 没确认干净的设备。恢复验证后由 `clear_unavailable()` 还。
                self._unavailable[session.device] = {
                    "device": session.device,
                    "session_id": session.session_id,
                    "error_reason": error_reason,
                    "closed_reason": reason,
                    "reservation_id": session.reservation_id,
                    "at": now_iso(),
                    "lease": session.lease,
                }
        if error_reason is None:
            session.lease.release()
            logger.info(
                f"device console closed: session={session.session_id} device={session.device} "
                f"reason={reason} actions={session.action_count}"
            )
        else:
            logger.error(
                f"device console cleanup failed: session={session.session_id} "
                f"device={session.device} reason={reason} error_reason={error_reason}; "
                f"设备保持不可分配且未还锁，等恢复验证"
            )
        return record

    # ── 控制面预留（§十三） ───────────────────────────────────────────────────
    def _reservation_owner(self, session: _Session) -> dict[str, Any]:
        # 一期没有 `access_grants` 表（BL-104），grant 标识就是创建请求 ID；
        # 没有请求 ID 时退回 session_id，owner 字段不会是空的。
        return {
            "kind": "device_console",
            "grant_id": session.request_id or session.session_id,
            "session_id": session.session_id,
            "request_id": session.request_id,
            "device": session.device,
        }

    def _release_reservation(self, session: _Session, *, reason: str) -> None:
        reservation_id = session.reservation_id
        if not reservation_id:
            return
        try:
            self.reservations.release(reservation_id, actor=f"device_console:{reason}")
        except Exception as exc:  # noqa: BLE001 - 释放失败进对账，可重试
            self._note_reconciliation({
                "kind": "reservation_release_failed",
                "device": session.device,
                "session_id": session.session_id,
                "reservation_id": reservation_id,
                "error": str(exc),
            })
            logger.error(
                f"device console reservation release failed: session={session.session_id} "
                f"reservation={reservation_id}: {exc}"
            )
            return
        session.reservation_id = None

    def _note_reconciliation(self, entry: dict[str, Any]) -> None:
        with self._guard:
            self._reconciliation.append({**entry, "at": now_iso()})

    def pending_reconciliation(self) -> list[dict[str, Any]]:
        """待对账项：预留登记结果未知、预留释放失败。"""
        with self._guard:
            return [dict(entry) for entry in self._reconciliation]

    def retry_reconciliation(self) -> dict[str, Any]:
        """重试对账清单里可重试的项（当前只有「释放失败」一类）。"""
        with self._guard:
            pending = list(self._reconciliation)
            self._reconciliation = []
        remaining: list[dict[str, Any]] = []
        released = 0
        for entry in pending:
            if entry.get("kind") != "reservation_release_failed":
                remaining.append(entry)
                continue
            try:
                self.reservations.release(
                    str(entry.get("reservation_id") or ""), actor="device_console:reconcile",
                )
            except Exception as exc:  # noqa: BLE001 - 还是失败就留在清单里
                remaining.append({**entry, "error": str(exc)})
                continue
            released += 1
        with self._guard:
            self._reconciliation = remaining + self._reconciliation
        return {"released": released, "pending": len(remaining)}

    # ── 不可分配设备（§8.4） ──────────────────────────────────────────────────
    def unavailable_devices(self) -> list[dict[str, Any]]:
        with self._guard:
            return [
                {k: v for k, v in entry.items() if k != "lease"}
                for entry in self._unavailable.values()
            ]

    def clear_unavailable(self, device: str, *, actor: str) -> dict[str, Any]:
        """恢复验证通过后把设备放回可分配：还掉被扣住的 lease，清掉记录。

        只由人（或恢复流程）显式调用——「清理失败」不会自愈，自动清除等于把
        §8.4 的隔离撤掉。
        """
        with self._guard:
            entry = self._unavailable.pop(device, None)
        if entry is None:
            raise DeviceConsoleError(
                f"device is not marked unavailable: {device}", status=404, code="not_found",
            )
        lease = entry.pop("lease", None)
        reservation_id = str(entry.get("reservation_id") or "")
        if reservation_id:
            try:
                self.reservations.release(reservation_id, actor=f"device_console:recovered:{actor}")
            except Exception as exc:  # noqa: BLE001 - 释放失败进对账，不挡恢复
                self._note_reconciliation({
                    "kind": "reservation_release_failed",
                    "device": device,
                    "session_id": entry.get("session_id"),
                    "reservation_id": reservation_id,
                    "error": str(exc),
                })
                logger.error(
                    f"device console reservation release failed on recovery: device={device} "
                    f"reservation={reservation_id}: {exc}"
                )
        if lease is not None:
            lease.release()
        logger.info(
            f"device console unavailable cleared: device={device} actor={actor} "
            f"error_reason={entry.get('error_reason')}"
        )
        return {**entry, "cleared_by": actor, "cleared_at": now_iso()}

    def _remember_ack(self, session: _Session, event_id: str, ack: dict[str, Any], digest: str) -> None:
        session.acks[event_id] = {**ack, "intent_hash": digest}
        while len(session.acks) > ACK_HISTORY:
            session.acks.popitem(last=False)

    # ── public API ───────────────────────────────────────────────────────────
    def start(self, device: str, *, request_id: str | None = None) -> dict[str, Any]:
        """开一条会话。带 `request_id` 时按请求 ID 幂等（§8.2）。

        - 同一 `request_id` 并发或重试：后来者等第一次调用出结论，拿回**同一个**
          session，不二次占锁；
        - 第一次调用已明确失败（如 `device_busy`——可以确定没占到设备）时不缓存结论，
          重试按新请求处理；只有成功的结论会被记住。这与「结果未知时禁止另分配一台」
          不冲突：那说的是**结果未知**，明确失败不是未知；
        - 原会话已经结束时，同 ID 重试拿到 410 `session_closed`，不会另开一条——
          「不对同一请求另分配一台」；
        - 不带 `request_id` 时行为与本条之前一致：没有跨调用幂等。web 侧会自己生成
          一个（`autophone/adapters/http/app.py`），所以页面路径恒带 ID。
        """
        device = str(device or "").strip()
        if not device:
            raise DeviceConsoleError("device is required", status=422, code="validation")
        request_id = str(request_id or "").strip() or None
        if request_id is None:
            return self._start_session(device, None)

        with self._guard:
            pending = self._requests.get(request_id)
            owner = pending is None
            if owner:
                pending = _StartRequest(request_id=request_id)
                self._requests[request_id] = pending
                while len(self._requests) > REQUEST_HISTORY:
                    self._requests.popitem(last=False)
        if pending is None:  # pragma: no cover - 上面两条分支都赋了值
            raise DeviceConsoleError("console start bookkeeping is inconsistent", status=500)
        if not owner:
            return self._await_start(pending)
        try:
            result = self._start_session(device, request_id)
        except BaseException as exc:
            with self._guard:
                # 明确失败：结论只交给正在等的那批调用者，不留在表里挡住后续重试。
                self._requests.pop(request_id, None)
            pending.error = exc
            pending.done.set()
            raise
        pending.result = dict(result)
        pending.session_id = str(result["session_id"])
        pending.done.set()
        return result

    def _await_start(self, pending: _StartRequest) -> dict[str, Any]:
        if not pending.done.wait(timeout=self.start_wait_s):
            # 结果未知：grant 停在 creating，调用方**不得**对同一请求另分配一台。
            raise DeviceConsoleError(
                f"console session for request {pending.request_id} is still being created",
                status=409, code="session_creating",
            )
        if pending.error is not None:
            raise pending.error
        result = dict(pending.result or {})
        session_id = str(result.get("session_id") or "")
        with self._guard:
            live = session_id in self._sessions
            closed = self._closed.get(session_id)
        if not live:
            raise DeviceConsoleError(
                f"console session已结束（{(closed or {}).get('reason')}）",
                status=410, code="session_closed",
            )
        return result

    def _start_session(self, device: str, request_id: str | None) -> dict[str, Any]:
        with self._guard:
            unavailable = self._unavailable.get(device)
        if unavailable is not None:
            # 上一条会话清理失败，设备还没过恢复验证（§8.4）。
            raise DeviceConsoleError(
                f"device is not allocatable: {unavailable.get('error_reason')}",
                status=409, code="device_unavailable",
            )
        # 桌面 Tk 录制现在自持真 lease（owner_kind=desktop_recording），互斥由下面的
        # try_lease 一并覆盖——不再需要单独看它的 holder sidecar。
        session_id = f"ctl-{secrets.token_hex(6)}"
        lease = self.lock.try_lease(device=device, owner_kind="web_control", owner_id=session_id)
        if lease is None:
            owner = self.lock.status(device=device)
            raise DeviceConsoleError(
                f"device is busy: {owner.get('owner_kind') or 'workflow'} "
                f"{owner.get('owner_id') or owner.get('run_id')}",
                code="device_busy",
            )
        token = secrets.token_urlsafe(32)
        try:
            driver = self.driver_factory()
            data, width, height = driver.screenshot()
        except Exception as exc:  # noqa: BLE001 — no device, no console
            lease.release()
            raise DeviceConsoleError(str(exc), status=503, code="device_error") from exc
        if (width, height) != EXPECTED_PREVIEW:
            lease.release()
            # Fail closed exactly like RecordSession: a rescaled frame would
            # mis-map every tap the operator makes.
            raise DeviceConsoleError(
                f"screenshot dims {(width, height)} != expected preview {EXPECTED_PREVIEW}",
                status=503, code="geometry_error",
            )
        now = self.clock()
        session = _Session(
            session_id=session_id,
            device=device,
            token_hash=_token_hash(token),
            lease=lease,
            driver=driver,
            geometry={
                "preview": list(EXPECTED_PREVIEW),
                "native": [width * 3, height * 3],
                "native_scale": 3,
            },
            started_at=now_iso(),
            heartbeat_at=now,
            request_id=request_id,
        )
        session.frame_seq = 1
        session.frame_data = data
        session.frame_at = now
        session.frame_times.append((1, now))
        try:
            session.reservation_id = self.reservations.create(
                device=device, owner=self._reservation_owner(session),
            )
        except Exception as exc:  # noqa: BLE001 - 登记失败即创建失败
            # 补偿选「回滚 rig 侧会话」：本次会话一个动作都没派发过，还锁不会打断任何
            # 在飞动作，设备立刻干净可用；选「标记不可分配」会把一台确实健康的设备
            # 隔离掉。代价是登记可能已经成功但回包丢了，那条预留会变成「有预留无
            # lease」，进对账清单由 `retry_reconciliation()` 或控制面对账释放。
            lease.release()
            self._note_reconciliation({
                "kind": "reservation_register_unknown",
                "device": device,
                "session_id": session_id,
                "request_id": request_id,
                "error": str(exc),
            })
            logger.error(
                f"device console reservation register failed: session={session_id} "
                f"device={device}: {exc}; 已回滚会话并还锁，预留登记结果未知进对账"
            )
            raise DeviceConsoleError(
                f"could not register the control plane reservation: {exc}",
                status=503, code="reservation_failed",
            ) from exc
        with self._guard:
            session.lifecycle = LIFECYCLE_ACTIVE
            self._sessions[session_id] = session
        logger.info(
            f"device console started: session={session_id} device={device} "
            f"request_id={request_id} reservation={session.reservation_id}"
        )
        return {
            "session_id": session_id,
            "device": device,
            "controller_token": token,
            # `state` 是既有线上字段（前端只显示不判断）；`lifecycle` 是 §8.1 的四态。
            "state": "live",
            "lifecycle": LIFECYCLE_ACTIVE,
            "request_id": request_id,
            "reservation_id": session.reservation_id,
            "started_at": session.started_at,
            "geometry": session.geometry,
        }

    def view(self, session_id: str, *, device: str | None = None) -> dict[str, Any]:
        session = self._get(session_id, device)
        return {
            "session_id": session.session_id,
            "device": session.device,
            "state": "closing" if session.lifecycle == LIFECYCLE_CLOSING else "live",
            "lifecycle": session.lifecycle,
            "request_id": session.request_id,
            "reservation_id": session.reservation_id,
            "started_at": session.started_at,
            "seq": session.frame_seq,
            "action_count": session.action_count,
            "last_action_at": session.last_action_at,
            "inflight": {"event_id": session.inflight["event_id"]} if session.inflight else None,
            "idle_s": round(max(0.0, self.clock() - session.heartbeat_at), 1),
            "geometry": session.geometry,
        }

    def frame(self, session_id: str, *, device: str | None = None) -> tuple[bytes, int, float]:
        session = self._get(session_id, device)
        with session.frame_gate:
            now = self.clock()
            if session.frame_data is None or now - session.frame_at >= self.frame_min_interval_s:
                try:
                    data, width, height = session.driver.screenshot()
                except Exception as exc:  # noqa: BLE001
                    raise DeviceConsoleError(str(exc), status=503, code="device_error") from exc
                if (width, height) != EXPECTED_PREVIEW:
                    raise DeviceConsoleError(
                        f"screenshot dims {(width, height)} != expected preview {EXPECTED_PREVIEW}",
                        status=503, code="geometry_error",
                    )
                now = self.clock()
                session.frame_seq += 1
                session.frame_data = data
                session.frame_at = now
                session.frame_times.append((session.frame_seq, now))
            return session.frame_data, session.frame_seq, session.frame_at

    def event(
        self, session_id: str, payload: dict[str, Any], token: str | None,
        *, device: str | None = None,
    ) -> dict[str, Any]:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id or len(event_id) > 128:
            raise DeviceConsoleError("event_id is required", status=422, code="validation")
        try:
            intent = normalize_preview_intent(payload)
            frame_seq = int(payload["frame_seq"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DeviceConsoleError(str(exc), status=422, code="validation") from exc
        digest = _intent_hash(intent)
        session = self._get(session_id, device)
        # 两道存活检查：这一道拒掉「已经在 closing 了还提交动作」；gate 内那一道拒掉
        # 「取到会话引用之后才被关闭」——两者之间正是原实现漏掉的那个窗口（§8.3）。
        self._reject_unless_active(session)
        with self._execution(session):
            self._reject_unless_active(session)
            self._check_token(session, token)
            previous = session.acks.get(event_id)
            if previous is not None:
                if previous.get("intent_hash") != digest:
                    raise DeviceConsoleError(
                        "event_id reused with different intent", code="idempotency_conflict",
                    )
                # Replay of an action that already reached (or already failed on)
                # the device: hand back the same verdict, never dispatch twice.
                return {k: v for k, v in previous.items() if k != "intent_hash"}
            with session.frame_gate:
                frame_at = next((at for seq, at in session.frame_times if seq == frame_seq), None)
            if frame_at is None or self.clock() - frame_at > self.frame_max_age_s:
                raise DeviceConsoleError("frame is stale", code="stale_frame")
            session.inflight = {"event_id": event_id, "intent_hash": digest}
            action_at = now_iso()
            try:
                if intent["kind"] == "tap":
                    session.driver.tap(intent["px"], intent["py"])
                elif intent["kind"] == "type":
                    session.driver.type_text(intent["text"])
                elif intent["kind"] == "key":
                    session.driver.press_key(intent["name"])
                else:
                    session.driver.swipe(
                        intent["px1"], intent["py1"], intent["px2"], intent["py2"], ms=intent["ms"],
                    )
            except Exception as exc:  # noqa: BLE001
                # The dispatch may or may not have landed — that is exactly why
                # the id is burned: a resend must not gamble a second action.
                ack = {
                    "event_id": event_id, "index": session.action_count, "kind": intent["kind"],
                    "ok": False, "action_at": action_at, "error": str(exc),
                }
                self._remember_ack(session, event_id, ack, digest)
                session.inflight = None
                logger.warning(
                    f"device console action failed: session={session_id} kind={intent['kind']}: {exc}"
                )
                raise DeviceConsoleError(str(exc), status=503, code="device_error") from exc
            ack = {
                "event_id": event_id,
                "index": session.action_count,
                "kind": intent["kind"],
                "ok": True,
                "action_at": action_at,
            }
            self._remember_ack(session, event_id, ack, digest)
            session.action_count += 1
            session.last_action_at = action_at
            session.inflight = None
            session.heartbeat_at = self.clock()
            # 动作成功后使缓存失效；失效写入与在途截图发布互斥。
            with session.frame_gate:
                session.frame_data = None
            logger.debug(
                f"device console {intent['kind']}: session={session_id} index={ack['index']}"
            )
            return ack

    def heartbeat(
        self, session_id: str, token: str | None, *, device: str | None = None,
    ) -> dict[str, Any]:
        session = self._get(session_id, device)
        self._check_token(session, token)
        # 已在 closing 的会话不刷心跳：回 ok 会让页面以为一切正常，而设备正在被收回。
        self._reject_unless_active(session)
        session.heartbeat_at = self.clock()
        return {"ok": True, "heartbeat_at": session.heartbeat_at}

    def stop(
        self, session_id: str, token: str | None, *, device: str | None = None,
        reason: str = "stopped",
    ) -> dict[str, Any]:
        """结束会话。`reason` 让到期与撤销（§7.2 第 5 条）复用同一条路径。"""
        session = self._get(session_id, device)
        self._check_token(session, token)
        # 等在飞动作落地、做清理、再还锁，全在 `_close` 里（§8.3 的同一条清理流程）：
        # 在飞动作还没落地就把 lease 还回去，run 会在它落地之前拿到设备——那一下 tap
        # 就打在别人的 run 上。等不到就按 §8.4 记 error_reason、设备进不可分配，
        # 用户这一侧的访问照样结束。
        return self._close(session, reason=reason or "stopped")

    def device_status(self, device: str) -> dict[str, Any]:
        """Device-level view for the dashboard: 现在有没有人在页面上操作这台设备。"""
        with self._guard:
            session = next((s for s in self._sessions.values() if s.device == device), None)
        if session is None:
            return {"device": device, "active": False}
        return {
            "device": device,
            "active": True,
            "session_id": session.session_id,
            "lifecycle": session.lifecycle,
            "started_at": session.started_at,
            "action_count": session.action_count,
            "idle_s": round(max(0.0, self.clock() - session.heartbeat_at), 1),
        }

    def expire_heartbeats(self) -> None:
        now = self.clock()
        for session in list(self._sessions.values()):
            if now - session.heartbeat_at <= self.heartbeat_ttl_s:
                continue
            if session.gate.locked():
                # 同 stop：动作还在飞就先不还锁，下一轮扫描（15s 后）再收。
                continue
            # 浏览器走了（关页/断网/机器睡了），设备不能一直被这条会话占着。
            self._close(session, reason="heartbeat_timeout")
