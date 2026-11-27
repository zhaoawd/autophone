"""B 端接入的设备分配与会话生命周期（BL-104）。

对应 docs/platform-device-console-embed.md §六（VM 池与分配）、§七（生命周期）、
§10.1（四条 B 端后端接口）。本模块是这四条接口的**全部业务逻辑**，`api.py` 只做
HTTP 协议转换。

三个协作者全部注入，所以整条分配链路可以在没有设备、没有控制面的情况下测：

- `access`（`EmbedAccessRegistry`）：接入方登记、启动凭证、授权记录存放；
- `call(device, op, payload)`：交互通道（BL-100）。控制面已配置时它把操作转发给
  持设备的 rig，未配置时在本进程执行，两条路返回形状逐字相同；
- `connected()`：通道上当前连着哪些设备。返回 `None` 表示「不知道」——单 VM 兼容
  路径没有通道注册表，此时不拿这一项过滤（本进程就是设备持有者）。

**分配算法**（§6.2，一期）：

1. 候选集 = 接入方设备池；请求带 `device` 时候选集只有该设备（且必须在池内）；
2. 过滤：在线、通道已连、ScreenLock 无持有者；
3. 按候选顺序尝试 `console_start`，撞 `device_busy` 换下一台。并发申请由 ScreenLock
   与 `uq_active_grant_per_device` 仲裁，应用层不加锁；
4. 首次成功即返回；全部失败返回 `device_busy`，附各候选的持有者类型与开始时间，
   **不自动降级观看**。

`mode=view` 只做步 1 与「在线 + 通道已连」这两项过滤，不做步 3，也**不**因为设备
上有 control 授权或 ScreenLock 持有者就把它排除（2026-09-09 裁定：
`uq_active_grant_per_device` 加了 `mode = 'control'` 条件，一台设备上可以并存一条
control 授权与任意多条 view 授权）。观看他人会话仍以 §6.1 的
`shared_account=true` 且持有者同接入方为前提，见 `_create_view`。

**创建幂等**（§8.2）：同一 `request_id` 返回同一结果。`access_grants.request_id` 上的
唯一约束是这条契约的实现，不是另一层缓存。结果未知（grant 停在 `creating`）时拒绝
对同一请求另分配一台，返回 `session_creating`。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from autophone.application.access_grants import (
    GRANT_ACTIVE,
    GRANT_CLOSED,
    GRANT_CLOSING,
    GRANT_CREATING,
    GRANT_EVENT_KINDS,
    GRANT_OPEN_STATUSES,
    GrantConflict,
    GrantEventSpec,
    GrantRecord,
    new_grant_id,
    open_grants_by_device,
)
from autophone.application.embed_access import (
    MODES,
    AccessPlatform,
    EmbedAccessRegistry,
)

logger = logging.getLogger(__name__)

# 会话默认有效期与上限。上限存在的理由：到期是设备回收的兜底之一（§8.5），
# B 端能签一条无限长的会话就等于取消这条兜底。
DEFAULT_SESSION_TTL_S = 3600.0
MAX_SESSION_TTL_S = 8 * 3600.0

# 嵌入页路径。B 端拿到的 `embed_url` 是它加上 device / mode / 一次性启动凭证。
EMBED_PATH = "/embed/device-console"

# console 会话已经不在了的两个通道错误码：撤销时见到它们说明 rig 侧早已清理完，
# 授权记录可以直接进终态，不算清理失败。
_SESSION_GONE_CODES = frozenset({"session_closed", "not_found"})


class DeviceAccessError(Exception):
    """B 端接口的结构化拒绝：`status` 与 `code` 直接进 HTTP 响应。

    `extra` 里放 code 特有的字段，当前只有 `device_busy` 的 `candidates`
    （各候选的持有者类型与开始时间，§6.2 步 4）。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 409,
        code: str = "conflict",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = dict(extra or {})

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.extra}


class DeviceAccessService:
    """四条 B 端接口的业务面。"""

    def __init__(
        self,
        *,
        access: EmbedAccessRegistry,
        call: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        connected: Callable[[], set[str] | None] | None = None,
        clock: Callable[[], float] = time.time,
        default_ttl_s: float = DEFAULT_SESSION_TTL_S,
        max_ttl_s: float = MAX_SESSION_TTL_S,
        embed_base_url: str = "",
    ) -> None:
        self.access = access
        self._call = call
        self._connected = connected
        self.clock = clock
        self.default_ttl_s = float(default_ttl_s)
        self.max_ttl_s = float(max_ttl_s)
        self.embed_base_url = embed_base_url.rstrip("/")

    # ── GET /api/access/devices ──────────────────────────────────────────────
    def devices(self, platform: AccessPlatform) -> list[dict[str, Any]]:
        """接入方可见设备列表：在线、通道连接、持有者（§6.3）。

        autophone 不替 B 端画选择器，所以这里只给这三项事实加一条「本接入方是否已经
        有一条授权占着它」，不做推荐、不排序。
        """
        connected = self._connected_devices()
        # 一台设备上可以并存一条 control 授权与多条 view 授权，这一列问的是「谁占着
        # 它」，所以固定取 control 那条；没有 control 时取第一条 view。
        grants: dict[str, GrantRecord] = {}
        for row in self.access.grants.list(
            statuses=GRANT_OPEN_STATUSES, devices=list(platform.device_pool) or None,
        ):
            current = grants.get(row.device)
            if current is None or (row.mode == "control" and current.mode != "control"):
                grants[row.device] = row
        rows: list[dict[str, Any]] = []
        for device in platform.device_pool:
            status, error = self._device_status(device)
            lock = dict(status.get("lock") or {})
            grant = grants.get(device)
            rows.append({
                "device": device,
                "online": bool(status.get("online")),
                "channel_connected": (True if connected is None else device in connected),
                "holder": _holder_view(lock),
                "grant": (
                    {
                        "grant_id": grant.grant_id,
                        "platform": grant.platform_id,
                        "subject": grant.subject,
                        "mode": grant.mode,
                        "status": grant.status,
                    }
                    if grant is not None else None
                ),
                "error": error,
            })
        return rows

    # ── POST /api/access/sessions ────────────────────────────────────────────
    def create_session(
        self,
        platform: AccessPlatform,
        *,
        request_id: str,
        user: str,
        mode: str,
        device: str | None = None,
        ttl_s: float | None = None,
    ) -> dict[str, Any]:
        """分配一台设备并建会话（§10.1）。"""
        request_id = str(request_id or "").strip()
        subject = str(user or "").strip()
        mode = str(mode or "").strip()
        if not request_id:
            raise DeviceAccessError("request_id is required", status=422, code="validation")
        if not subject:
            raise DeviceAccessError("user is required", status=422, code="validation")
        if mode not in MODES:
            raise DeviceAccessError(
                "mode must be control or view", status=422, code="validation",
            )
        ttl = self._ttl(ttl_s)

        existing = self.access.grants.find(request_id=request_id)
        if existing is not None:
            return self._replay(platform, existing)

        candidates, busy = self._candidates(platform, device, mode)
        if not candidates:
            # 一台都没通过过滤：本次请求**确定**没占到任何设备，所以不落 grant 行，
            # 同一个 `request_id` 重试会重新评估（明确失败不是「结果未知」，§8.2）。
            raise DeviceAccessError(
                f"no device in the pool of {platform.platform_id} is available",
                code="device_busy", extra={"candidates": busy},
            )
        if mode == "view":
            return self._create_view(platform, candidates, subject, request_id, ttl)
        return self._create_control(platform, candidates, subject, request_id, ttl, busy)

    # ── GET /api/access/sessions/{id} ────────────────────────────────────────
    def get_session(self, platform: AccessPlatform | None, ident: str) -> dict[str, Any]:
        grant = self._lookup(platform, ident)
        return self._session_view(grant)

    # ── DELETE /api/access/sessions/{id} ─────────────────────────────────────
    def revoke_session(
        self,
        platform: AccessPlatform | None,
        ident: str,
        *,
        reason: str = "revoked",
        actor: str | None = None,
    ) -> dict[str, Any]:
        """撤销（B 端 DELETE）与踢出（看板管理员）共用的关闭路径（§7.2 第 5 条）。

        顺序：grant 进 `closing`（凭证立刻失效，页面下一次请求即 401）→ 调
        `console_stop`，rig 侧在同一条清理流程里等在飞动作落地、复位、释放预留、还锁
        → 按结果把 grant 推到 `closed`，或按 §8.4 留在 `closing` 并记 `error_reason`。

        **留在 `closing` 是有意的**：`uq_active_grant_per_device` 覆盖 `closing`，
        清理没确认完成的设备因此在 PG 侧也分配不出去，与 rig 侧「不可分配且不还锁」
        对齐。恢复由人走 `clear_unavailable()`，两侧一起撤。

        **事件（BL-116）**：`reason` 属于 `GRANT_EVENT_KINDS`（`revoked` / `kicked` /
        `expired`）时，进入 `closing` 的那次写入同时向只追加的 `access_grant_events`
        写一行，两者同一事务。已是 `closed` 的授权不再写事件（没有发生关闭操作）；
        对 `closing` 的重复撤销会再写一行，`from_status` 为 `closing`。`actor` 缺省时
        按入口推断：带接入方为 `platform:<id>`，到期为 `system:expiry`，其余为 `dashboard`。
        """
        grant = self._lookup(platform, ident)
        if grant.status == GRANT_CLOSED:
            return self._session_view(grant)
        event: GrantEventSpec | None = None
        if reason in GRANT_EVENT_KINDS:
            event = GrantEventSpec(
                kind=reason,
                actor=actor or _default_actor(platform, reason),
                reason=reason,
                details={"requested_ident": str(ident or "")},
            )
        grant = self.access.grants.update(
            grant.grant_id, event=event, status=GRANT_CLOSING, closed_reason=reason,
        )
        session_id = grant.console_session_id if grant.mode == "control" else None
        if not session_id:
            # 观看授权：它可能绑着别人的 console 会话（§7.5），但那条 lease 不是它的，
            # 撤销一个观看者不得把持有者的会话停掉，所以任何 view 授权都直接进终态。
            # control 授权没开过会话时同理，没有 lease 要还。
            closed = self.access.grants.update(
                grant.grant_id, status=GRANT_CLOSED, closed_reason=reason,
            )
            return self._session_view(closed)

        error_reason: str | None = None
        try:
            result = self._call(grant.device, "console_stop", {
                "session_id": session_id,
                "controller_token": grant.controller_token,
                "reason": reason,
            })
        except Exception as exc:  # noqa: BLE001 - 通道 / 设备侧的任何失败
            code = str(getattr(exc, "code", "") or "")
            if code not in _SESSION_GONE_CODES:
                error_reason = f"console_stop_failed: {exc}"
            result = {}
        else:
            error_reason = str(result.get("error_reason") or "") or None

        if error_reason is not None:
            logger.error(
                f"device access revoke did not confirm cleanup: grant={grant.grant_id} "
                f"device={grant.device} reason={reason} error_reason={error_reason}; "
                f"授权记录留在 closing，设备保持不可分配"
            )
            held = self.access.grants.update(grant.grant_id, error_reason=error_reason)
            return self._session_view(held)
        closed = self.access.grants.update(
            grant.grant_id, status=GRANT_CLOSED, closed_reason=reason,
        )
        logger.info(
            f"device access revoked: grant={closed.grant_id} device={closed.device} "
            f"session={session_id} reason={reason}"
        )
        return self._session_view(closed)

    # ── 到期（§8.5） ─────────────────────────────────────────────────────────
    def expire_grant(self, grant: GrantRecord) -> dict[str, Any]:
        """一条已过期的授权：拒绝动作与心跳之外，还要真的把设备收回来。

        由会话路由的准入闸在发现过期时调用。`revoke_session` 会先把 grant 推到
        `closing`，所以同一条会话上后续并发请求看到的是「已在关闭」，不会重复触发。
        """
        if grant.status not in GRANT_OPEN_STATUSES or grant.status == GRANT_CLOSING:
            return self._session_view(grant)
        return self.revoke_session(None, grant.grant_id, reason="expired")

    # ── 内部 ─────────────────────────────────────────────────────────────────
    def _ttl(self, ttl_s: float | None) -> float:
        if ttl_s is None:
            return self.default_ttl_s
        try:
            value = float(ttl_s)
        except (TypeError, ValueError) as exc:
            raise DeviceAccessError(
                "ttl_s must be a number", status=422, code="validation",
            ) from exc
        if value <= 0:
            raise DeviceAccessError("ttl_s must be positive", status=422, code="validation")
        return min(value, self.max_ttl_s)

    def _connected_devices(self) -> set[str] | None:
        if self._connected is None:
            return None
        try:
            return self._connected()
        except Exception as exc:  # noqa: BLE001 - 读不出通道注册表就不拿它过滤
            logger.warning(f"device access: could not read the device channel registry: {exc}")
            return None

    def _device_status(self, device: str) -> tuple[dict[str, Any], str | None]:
        try:
            return dict(self._call(device, "device_status", {})), None
        except Exception as exc:  # noqa: BLE001 - 问不到状态即当它不可用
            return {}, str(exc)

    def _candidates(
        self, platform: AccessPlatform, device: str | None, mode: str = "control",
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """按 §6.2 步 1、2 算候选集，同时收集被过滤掉那些的持有者信息。

        `mode` 决定过滤到哪一层：

        - `control`：在线、通道已连、ScreenLock 无持有者、PG 侧无未关闭 control 授权；
        - `view`：只要在线且通道已连。观看不占 lease，也不再撞设备互斥索引
          （2026-09-09 裁定），所以有人在操作的设备照样是观看候选。
        """
        wanted = str(device or "").strip()
        if wanted:
            if wanted not in platform.device_pool:
                raise DeviceAccessError(
                    f"device {wanted} is not in the pool of {platform.platform_id}",
                    status=403, code="device_not_in_pool",
                )
            pool: Sequence[str] = (wanted,)
        else:
            pool = platform.device_pool
        if not pool:
            raise DeviceAccessError(
                f"platform {platform.platform_id} has no device pool configured",
                code="device_pool_empty",
            )
        connected = self._connected_devices()
        # 只看 control 授权：view 授权不占设备（`uq_active_grant_per_device` 自
        # 2026-09-09 起带 `mode = 'control'` 条件），拿它挡分配会凭空少一台候选。
        open_grants = {
            row.device: row
            for row in self.access.grants.list(statuses=GRANT_OPEN_STATUSES, devices=list(pool))
            if row.mode == "control"
        }
        candidates: list[str] = []
        blocked: list[dict[str, Any]] = []
        for candidate in pool:
            status, error = self._device_status(candidate)
            if error is not None or not status.get("online"):
                blocked.append({
                    "device": candidate, "reason": "offline", "error": error,
                })
                continue
            if connected is not None and candidate not in connected:
                blocked.append({"device": candidate, "reason": "channel_disconnected"})
                continue
            if mode == "view":
                # 观看不做步 3，也不判占用：设备被别人操作正是观看的常见前提。
                candidates.append(candidate)
                continue
            lock = dict(status.get("lock") or {})
            if lock.get("locked"):
                blocked.append({
                    "device": candidate, "reason": "device_busy", "holder": _holder_view(lock),
                })
                continue
            grant = open_grants.get(candidate)
            if grant is not None and grant.status == GRANT_ACTIVE:
                # 设备上没有 lease，表里却还挂着一条 `active` 授权：那条会话已经被
                # rig 侧回收了（心跳超时、rig 重启），记录是残留。清掉它，否则这台
                # 设备在 PG 侧永远分配不出去。`creating` 与 `closing` 不动，理由见
                # `close_open_grant_for_device`。
                self.access.close_open_grant_for_device(candidate, reason="session_gone")
                grant = None
            if grant is not None:
                # PG 侧还挂着一条没关完的授权（含 `closing`）：设备不可分配（§十一）。
                blocked.append({
                    "device": candidate,
                    "reason": "grant_open",
                    "holder": {
                        "owner_kind": "web_control",
                        "platform": grant.platform_id,
                        "subject": grant.subject,
                        "grant_status": grant.status,
                    },
                })
                continue
            candidates.append(candidate)
        return candidates, blocked

    def _create_view(
        self,
        platform: AccessPlatform,
        candidates: list[str],
        subject: str,
        request_id: str,
        ttl: float,
    ) -> dict[str, Any]:
        """`mode=view` 不做 `console_start`（§6.2 末段），只签一份观看授权。

        绑不绑 console 会话由 §6.1 那条前提决定：**持有者同接入方且该接入方
        `shared_account=true`** 时，观看授权绑上持有者的 console 会话，嵌入页因此能
        取到帧；不满足（含没有持有者、`shared_account` 未声明）时授权照签但不带会话
        标识，嵌入页取不到帧——这是 BL-104 一期就有的行为，本次不改。

        授权本身不占 lease、不发动作：`controller_token` 恒为 `None`，撤销它也不会
        碰持有者的会话（见 `revoke_session`）。
        """
        device = candidates[0]
        session_id: str | None = None
        if platform.shared_account:
            holder = next(
                (
                    row for row in self.access.grants.list(
                        statuses=(GRANT_ACTIVE,), platform_id=platform.platform_id,
                        devices=[device],
                    )
                    if row.mode == "control" and row.console_session_id
                ),
                None,
            )
            session_id = holder.console_session_id if holder is not None else None
        now = self.clock()
        grant = self.access.grants.create(GrantRecord(
            grant_id=new_grant_id(),
            request_id=request_id,
            platform_id=platform.platform_id,
            subject=subject,
            device=device,
            mode="view",
            status=GRANT_ACTIVE,
            console_session_id=session_id,
            controller_token=None,
            expires_at=now + ttl,
            created_at=now,
        ))
        return self._session_view(grant, launch=True)

    def _create_control(
        self,
        platform: AccessPlatform,
        candidates: list[str],
        subject: str,
        request_id: str,
        ttl: float,
        blocked: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = self.clock()
        grant: GrantRecord | None = None
        busy = list(blocked)
        for device in candidates:
            grant, claimed = self._claim(
                grant, platform, subject, request_id, device, ttl, now, busy,
            )
            if not claimed:
                continue
            try:
                started = self._call(device, "console_start", {"request_id": request_id})
            except Exception as exc:  # noqa: BLE001
                code = str(getattr(exc, "code", "") or "")
                if code == "device_busy":
                    # §6.2 步 3：撞上就换下一台。过滤与 start 之间的这段窗口由
                    # ScreenLock 仲裁，这里只负责换人。
                    busy.append({
                        "device": device, "reason": "device_busy",
                        "holder": self._console_holder(device),
                    })
                    continue
                # 其余失败（设备错误、预留登记失败……）不换设备：换一台掩盖的是
                # 一个需要人看的故障，而不是一次正常的争用。
                self.access.grants.update(
                    grant.grant_id, status=GRANT_CLOSED,
                    closed_reason=code or "console_start_failed", error_reason=str(exc),
                )
                raise DeviceAccessError(
                    f"could not start a console session on {device}: {exc}",
                    status=int(getattr(exc, "status", 503) or 503),
                    code=code or "console_start_failed",
                ) from exc
            bound = self.access.grants.update(
                grant.grant_id,
                status=GRANT_ACTIVE,
                console_session_id=str(started.get("session_id") or ""),
                controller_token=started.get("controller_token"),
            )
            logger.info(
                f"device access granted: grant={bound.grant_id} platform={bound.platform_id} "
                f"subject={bound.subject} device={bound.device} mode=control "
                f"session={bound.console_session_id} request_id={request_id}"
            )
            return self._session_view(bound, launch=True, started=started)

        if grant is not None:
            # 有过 grant 行但每一台都被抢走：按 §8.2「同一 request_id 返回同一结果」，
            # 这条失败结论留在表里，重试同一个 ID 拿回同一个 `device_busy`。
            self.access.grants.update(
                grant.grant_id, status=GRANT_CLOSED, closed_reason="device_busy",
            )
        raise DeviceAccessError(
            f"every candidate device of {platform.platform_id} is busy",
            code="device_busy", extra={"candidates": busy},
        )

    def _claim(
        self,
        grant: GrantRecord | None,
        platform: AccessPlatform,
        subject: str,
        request_id: str,
        device: str,
        ttl: float,
        now: float,
        busy: list[dict[str, Any]],
    ) -> tuple[GrantRecord | None, bool]:
        """把授权记录落在某台候选设备上。

        返回 `(记录, 是否落在了这台设备上)`。撞设备互斥时第二项为假，调用方换下一台；
        记录本身照原样带回去（它可能还停在上一台候选上，也可能还没建）。

        一个 `request_id` 只有一行（表上唯一），所以换设备是 `UPDATE device`，
        不是再插一行。
        """
        try:
            if grant is None:
                return self.access.grants.create(GrantRecord(
                    grant_id=new_grant_id(),
                    request_id=request_id,
                    platform_id=platform.platform_id,
                    subject=subject,
                    device=device,
                    mode="control",
                    status=GRANT_CREATING,
                    expires_at=now + ttl,
                    created_at=now,
                )), True
            return self.access.grants.update(grant.grant_id, device=device), True
        except GrantConflict as conflict:
            if conflict.kind == "device":
                busy.append({"device": device, "reason": "grant_open"})
                return grant, False
            if conflict.kind == "subject":
                raise DeviceAccessError(
                    f"{platform.platform_id}/{subject} already holds a control session "
                    "(§7.4 一人一台)",
                    code="user_has_active_session",
                ) from conflict
            if conflict.kind == "request":
                # 同一 `request_id` 的并发创建：另一路已经写进去了，读回它的结论。
                raise DeviceAccessError(
                    f"request {request_id} is already being served",
                    code="session_creating",
                ) from conflict
            raise DeviceAccessError(
                f"could not record the access grant: {conflict}", status=500, code="grant_conflict",
            ) from conflict

    def _console_holder(self, device: str) -> dict[str, Any]:
        """`device_busy` 要附持有者类型与开始时间（§6.2 步 4）。"""
        holder: dict[str, Any] = {"owner_kind": "unknown"}
        try:
            status = dict(self._call(device, "device_status", {}))
        except Exception:  # noqa: BLE001 - 问不到就只说「被占用」，不猜持有者
            return holder
        lock = dict(status.get("lock") or {})
        return _holder_view(lock) or holder

    def _replay(self, platform: AccessPlatform, grant: GrantRecord) -> dict[str, Any]:
        """同一个 `request_id` 的重试：返回同一结果（§8.2）。"""
        if grant.platform_id != platform.platform_id:
            raise DeviceAccessError(
                f"request_id {grant.request_id} belongs to another platform",
                status=403, code="request_id_conflict",
            )
        if grant.status == GRANT_ACTIVE:
            return self._session_view(grant, launch=True)
        if grant.status == GRANT_CREATING:
            # 创建结果未知：**不**对同一请求另分配一台（§8.2）。
            raise DeviceAccessError(
                f"session for request {grant.request_id} is still being created",
                code="session_creating",
                extra={"grant_id": grant.grant_id, "device": grant.device},
            )
        if grant.closed_reason == "device_busy":
            raise DeviceAccessError(
                f"request {grant.request_id} already concluded: every candidate was busy",
                code="device_busy", extra={"candidates": []},
            )
        raise DeviceAccessError(
            f"session for request {grant.request_id} is {grant.status}"
            f"（{grant.closed_reason or '-'}）",
            status=410, code="session_closed",
            extra={"grant_id": grant.grant_id, "error_reason": grant.error_reason},
        )

    def _lookup(self, platform: AccessPlatform | None, ident: str) -> GrantRecord:
        """优先按授权标识查询；旧 console 标识只在唯一对应时兼容。"""
        value = str(ident or "").strip()
        if not value:
            raise DeviceAccessError("session id is required", status=422, code="validation")
        grant = self.access.grants.get(value)
        if grant is None:
            matches = [row for row in self.access.grants.list()
                       if row.console_session_id == value
                       and (platform is None or row.platform_id == platform.platform_id)]
            if len(matches) > 1:
                raise DeviceAccessError(
                    "console session has multiple grants; use the access session id",
                    status=409, code="ambiguous_session_id",
                )
            grant = matches[0] if matches else None
        if grant is None:
            raise DeviceAccessError(
                f"access session not found: {value}", status=404, code="not_found",
            )
        if platform is not None and grant.platform_id != platform.platform_id:
            # 不告诉调用方「它存在但不属于你」——按不存在处理。
            raise DeviceAccessError(
                f"access session not found: {value}", status=404, code="not_found",
            )
        return grant

    def _session_view(
        self,
        grant: GrantRecord,
        *,
        launch: bool = False,
        started: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """§10.1 的出参。`controller_token` 不在其中，任何分支都不在。"""
        view = dict(grant.public())
        # B 端会话的身份是授权记录；多个观看者可共享同一 console 会话。
        view["session_id"] = grant.grant_id
        view["console_session_id"] = grant.console_session_id
        if started is not None:
            view["started_at"] = started.get("started_at")
            view["geometry"] = started.get("geometry")
        if launch and grant.status == GRANT_ACTIVE:
            view["embed_url"] = self._embed_url(grant)
        return view

    def _embed_url(self, grant: GrantRecord) -> str:
        token, _ticket = self.access.issue_launch(
            platform_id=grant.platform_id,
            subject=grant.subject,
            device=grant.device,
            mode=grant.mode,
            console_session_id=grant.console_session_id,
            grant_id=grant.grant_id,
        )
        query = f"device={grant.device}&mode={grant.mode}&launch={token}"
        return f"{self.embed_base_url}{EMBED_PATH}?{query}"


def _default_actor(platform: AccessPlatform | None, reason: str) -> str:
    """事件的 actor 缺省值：调用方没给时按入口推断，不留空。"""
    if platform is not None:
        return f"platform:{platform.platform_id}"
    if reason == "expired":
        return "system:expiry"
    return "dashboard"


def _holder_view(lock: Mapping[str, Any]) -> dict[str, Any] | None:
    """ScreenLock 状态 → 对外的持有者视图：类型与开始时间（§6.2 步 4、§6.3）。"""
    if not lock.get("locked"):
        return None
    return {
        "owner_kind": lock.get("owner_kind") or "workflow",
        "owner_id": lock.get("owner_id") or lock.get("run_id"),
        "started_at": lock.get("acquired_at"),
    }


def reconcile_console_reservations(
    *,
    control: Any,
    call: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    grants: Any,
    devices: Iterable[str] | None = None,
    actor: str = "reconcile",
    apply: bool = True,
) -> dict[str, Any]:
    """`rig_reservations` 与 console 会话的对账（设计文档 §十三）。

    两条规则，各自的判据都是「rig 上此刻有没有 console lease」——用
    `console_device_status` 读，它是 lease 的直接投影（有会话即有 lease）：

    - **有预留无 lease → 释放预留**：owner 是 device_console 的 active 预留，
      设备上却没有 console 会话，说明关闭路径漏了释放（`_release_reservation` 失败
      进对账，或进程在两步之间退出）；
    - **有 lease 无预留 → 补登记**：设备上有 console 会话却没有 active 预留，
      Scheduler 会把 Assignment 派到正被人操作的 rig，要补一条。

    `apply=False` 只报不改，给「先看看要动什么」用。
    """
    reservations = [
        row for row in control.list_reservations(statuses=["active"])
        if str((row.get("owner") or {}).get("kind") or "") == "device_console"
    ]
    # 一台设备上可能并存 control 与 view 授权；预留的 owner 要记住持有者那条，
    # 所以按 control 优先取（`open_grants_by_device`）。
    open_grants = open_grants_by_device(grants)
    checked: set[str] = set(str(d).strip() for d in (devices or []) if str(d).strip())
    by_device: dict[str, dict[str, Any]] = {}
    for row in reservations:
        device = str((row.get("owner") or {}).get("device") or row.get("rig_id") or "")
        if device:
            by_device[device] = row
            checked.add(device)
    checked.update(open_grants)

    released: list[dict[str, Any]] = []
    registered: list[dict[str, Any]] = []
    for device in sorted(checked):
        try:
            status = dict(call(device, "console_device_status", {}))
        except Exception as exc:  # noqa: BLE001 - 读不到设备状态就不动它，留给下一轮
            logger.warning(f"reservation reconcile: could not read {device}: {exc}")
            continue
        has_lease = bool(status.get("active"))
        reservation = by_device.get(device)
        if reservation is not None and not has_lease:
            entry = {
                "device": device,
                "reservation_id": reservation.get("reservation_id"),
                "action": "release",
            }
            if apply:
                control.release_reservation(
                    str(reservation.get("reservation_id") or ""), f"reconcile:{actor}",
                )
            released.append(entry)
            continue
        if reservation is None and has_lease:
            grant = open_grants.get(device)
            owner = {
                "kind": "device_console",
                "grant_id": grant.grant_id if grant is not None else None,
                "session_id": status.get("session_id"),
                "device": device,
                "reconciled_by": actor,
            }
            entry = {"device": device, "action": "register", "owner": owner}
            if apply:
                row = control.create_reservation({"rig_id": device, "owner": owner})
                entry["reservation_id"] = row.get("reservation_id")
            registered.append(entry)
    return {
        "checked": sorted(checked),
        "released": released,
        "registered": registered,
        "applied": bool(apply),
    }


__all__ = [
    "DEFAULT_SESSION_TTL_S",
    "EMBED_PATH",
    "MAX_SESSION_TTL_S",
    "DeviceAccessError",
    "DeviceAccessService",
    "reconcile_console_reservations",
]
