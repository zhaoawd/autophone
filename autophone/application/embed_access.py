"""嵌入页启动凭证、会话访问凭证与接入方登记（BL-103 + BL-104）。

对应 docs/platform-device-console-embed.md §四对象模型与 §五鉴权。本模块只管**凭证**：
签发、校验、作废，以及接入方的允许来源。它不认识 HTTP、不认识设备驱动，也不碰
`DeviceConsoleManager` 的 `controller_token` 语义——`controller_token` 由调用方在
`console_start` 成功后交给本模块保管，本模块只负责让它留在 web 进程里。

三个对象：

- **接入方**（`AccessPlatform`）：一个 B 端系统的登记。一期只有标识与允许来源两项，
  凭证哈希、设备池、``shared_account`` 随 BL-104 补进同一份配置。
- **启动凭证**（`LaunchTicket`）：一次性令牌，写在嵌入 URL 的 ``launch`` 参数里，
  只能兑换一次会话访问凭证（§5.2）。兑换后记录保留到过期，好让页面刷新时仍能按
  接入方算出 ``frame-ancestors``，但再次兑换会被拒。
- **会话访问凭证**（`AccessGrant`）：嵌入页持有的短期令牌，绑定接入方、用户、设备、
  console 会话与模式，只出现在请求头里。

**存储形态（BL-104 起）**：授权记录搬到 `autophone.application.access_grants` 的
`GrantStore`——控制面已配置时是 `access_grants` 表，未配置时是进程内表（兼容路径）。
会话访问凭证因此改成**指针令牌**：``<grant_id>.<HMAC>``，本身不带任何授权信息，
校验时按 grant_id 回表读状态。撤销仍然立即生效（授权状态的真源是表，不是令牌），
BL-103 当初否掉自包含令牌的理由（过期前无法撤销）不再成立。

签名密钥取 ``AUTOPHONE_EMBED_TOKEN_KEY``；未设时每进程随机生成，此时令牌不跨进程
重启存活（授权记录本身仍在表里，重开页面重签即可）。

启动凭证仍在进程内：它是 120 秒的一次性票据，一次性要求一个可变的「已兑换」标记，
搬进表里只增加一张表而不改变任何契约（§5.2）。

**接入方登记**（§6.1，BL-104）：标识、凭证哈希、允许来源、设备池、``shared_account``
五项，来自同一份 ``AUTOPHONE_EMBED_PLATFORMS_FILE``（BL-103 已有的键名不变，本条只加
字段）。装载时校验设备池不重叠，重叠即拒绝加载。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from autophone.application.access_grants import (
    GRANT_ACTIVE,
    GRANT_CLOSED,
    GRANT_OPEN_STATUSES,
    GrantRecord,
    GrantStore,
    LazyGrantStore,
    new_grant_id,
)

logger = logging.getLogger(__name__)

# autophone 自己的看板也是一个接入方，标识固定；它的来源固定放行（§5.4）。
SELF_PLATFORM_ID = "autophone"
SELF_ORIGIN = "'self'"

MODES = ("control", "view")

DEFAULT_LAUNCH_TTL_S = 120.0
DEFAULT_ACCESS_TTL_S = 3600.0

# 授权记录状态的四个取值由 `access_grants` 模块定义并在本模块 import；这里不再重复。


class EmbedAccessError(Exception):
    """凭证被拒。``status`` 与 ``code`` 直接进 HTTP 响应，不再翻译一次。"""

    def __init__(self, message: str, *, status: int = 403, code: str = "embed_access_denied") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class AccessPlatform:
    """接入方登记（§6.1 的五项）。

    - ``allowed_origins``：``Content-Security-Policy: frame-ancestors`` 的取值来源，
      元素形如 ``https://host[:port]`` 或 ``'self'``。空列表表示这个接入方还没登记
      来源，此时拒绝签发启动凭证（§5.4）；
    - ``credential_hash``：平台凭证的 SHA-256 十六进制串（§5.5）。空表示这个接入方
      不能调 B 端后端接口，只能由 autophone 侧操作者替它签启动凭证；
    - ``device_pool``：这个接入方能分配到的设备键。设备池按接入方互斥，装载时校验；
    - ``shared_account``：池内 app 用共享测试账号。为真才允许同接入方观看他人会话
      （§6.1 最后一段）。
    """

    platform_id: str
    allowed_origins: tuple[str, ...] = ()
    credential_hash: str = ""
    device_pool: tuple[str, ...] = ()
    shared_account: bool = False

    def verify_credential(self, token: str) -> bool:
        """比对平台凭证。没登记哈希的接入方恒不通过——空哈希不是「任意口令都行」。"""
        if not self.credential_hash:
            return False
        return hmac.compare_digest(self.credential_hash, credential_hash(token))

    def public(self) -> dict[str, Any]:
        """能对外说的接入方字段。``credential_hash`` 不在其中。"""
        return {
            "platform": self.platform_id,
            "allowed_origins": list(self.allowed_origins),
            "device_pool": list(self.device_pool),
            "shared_account": self.shared_account,
        }


def credential_hash(token: str) -> str:
    """平台凭证的哈希取值：明文的 SHA-256 十六进制串。

    配置文件里写 ``sha256:<hex>`` 或裸 hex 都收，装载时统一成裸 hex。不加盐：这是
    一份运维手工登记的高熵服务凭证（不是用户口令），加盐要么把盐也写进同一份文件、
    要么另建密钥管理，两者都不增加实际强度。
    """
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


class AccessConfigError(RuntimeError):
    """接入方配置不合法，拒绝加载（§6.1：设备池重叠即拒绝加载）。"""


@dataclass
class LaunchTicket:
    """一次性启动凭证的记录。``used_at`` 非空即已兑换，不能再兑换。

    ``grant_id`` 非空表示这张票据指向一条**已经建好**的授权记录（B 端走
    `POST /api/access/sessions` 时的形态）：兑换只是给那条记录签一个访问凭证，
    不再新建记录，也不再开 console 会话。
    """

    ticket_id: str
    platform_id: str
    subject: str
    device: str
    mode: str
    expires_at: float
    console_session_id: str | None = None
    grant_id: str | None = None
    used_at: float | None = None


# BL-103 的 `AccessGrant` 就是 §十一 的一行，BL-104 起由 `access_grants` 模块承载。
# 保留旧名是为了不动调用点（本模块自身与 api.py 的类型标注）。
AccessGrant = GrantRecord


def normalize_origin(raw: str) -> str | None:
    """把一条来源规范成 ``scheme://host[:port]``；不合法返回 ``None``。

    只接受 http / https 的来源与字面量 ``'self'``；带路径、查询或缺主机的一律不收，
    因为 ``frame-ancestors`` 比对的是 origin，多余部分会让规则静默失配。
    """
    value = str(raw or "").strip()
    if not value:
        return None
    if value == SELF_ORIGIN:
        return SELF_ORIGIN
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _normalize_origins(raw_origins: Iterable[str], *, platform_id: str) -> tuple[str, ...]:
    origins: list[str] = []
    for raw in raw_origins:
        if not str(raw or "").strip():
            continue  # 空项来自 "a,,b" 之类的分隔，不是配错，不必告警
        origin = normalize_origin(raw)
        if origin is None:
            logger.warning(
                f"embed access: dropped an invalid allowed origin for platform={platform_id}: {raw!r}"
            )
            continue
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


class EmbedAccessRegistry:
    """接入方登记 + 启动凭证 + 会话访问凭证。

    授权记录本身不在这里：它由注入的 `GrantStore` 存放（控制面 PG 或进程内），
    本类只负责**签发与校验指向那条记录的凭证**，以及按接入方算 ``frame-ancestors``。

    线程安全：一把 `RLock` 护住接入方表与启动凭证表。`serve` 的路由跑在线程池里，
    同一条会话的帧轮询与心跳会并发进来。授权记录的并发由存放层保证。
    """

    def __init__(
        self,
        platforms: Mapping[str, AccessPlatform] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        launch_ttl_s: float = DEFAULT_LAUNCH_TTL_S,
        access_ttl_s: float = DEFAULT_ACCESS_TTL_S,
        grants: GrantStore | None = None,
        token_key: bytes | None = None,
    ) -> None:
        self.clock = clock
        self.launch_ttl_s = float(launch_ttl_s)
        self.access_ttl_s = float(access_ttl_s)
        self._guard = threading.RLock()
        self._platforms: dict[str, AccessPlatform] = dict(platforms or {})
        # autophone 自己的接入方永远在册，来源里永远有 'self'（§5.4）。
        existing = self._platforms.get(SELF_PLATFORM_ID)
        self_origins = (SELF_ORIGIN,) + tuple(
            origin for origin in (existing.allowed_origins if existing else ()) if origin != SELF_ORIGIN
        )
        self._platforms[SELF_PLATFORM_ID] = (
            AccessPlatform(
                SELF_PLATFORM_ID,
                self_origins,
                credential_hash=existing.credential_hash if existing else "",
                device_pool=existing.device_pool if existing else (),
                shared_account=existing.shared_account if existing else False,
            )
        )
        _assert_pools_disjoint(self._platforms)
        self._launches: dict[str, LaunchTicket] = {}
        self.grants: GrantStore = grants if grants is not None else LazyGrantStore()
        # 指针令牌的签名密钥；未显式给就随机生成，令牌因此不跨进程重启存活。
        self._token_key = token_key or secrets.token_bytes(32)

    # ── 接入方 ───────────────────────────────────────────────────────────────

    @property
    def platforms(self) -> dict[str, AccessPlatform]:
        with self._guard:
            return dict(self._platforms)

    def platform(self, platform_id: str) -> AccessPlatform:
        with self._guard:
            found = self._platforms.get(str(platform_id or "").strip())
        if found is None:
            raise EmbedAccessError(
                f"unknown embed platform: {platform_id}",
                status=404, code="embed_platform_unknown",
            )
        return found

    def authenticate_platform(self, token: str) -> AccessPlatform:
        """按平台凭证（``Authorization: Bearer``）认出接入方（§5.5）。

        比对的是配置文件里的哈希。没有任何接入方匹配就是 401——不回落到别的身份，
        更不因为「本机没配 `AUTOPHONE_AUTH`」而放行：B 端接口的准入与看板的开发模式
        是两件事。
        """
        value = str(token or "").strip()
        if not value:
            raise EmbedAccessError(
                "a platform credential is required",
                status=401, code="platform_credential_required",
            )
        with self._guard:
            platforms = list(self._platforms.values())
        for platform in platforms:
            if platform.verify_credential(value):
                return platform
        raise EmbedAccessError(
            "the platform credential is unknown",
            status=401, code="platform_credential_invalid",
        )

    def frame_ancestors(self, platform_id: str | None = None) -> str:
        """`Content-Security-Policy: frame-ancestors` 的取值（§5.4）。

        接入方不认识时退到 autophone 自己的来源：拿不准就只让自家看板嵌，
        比放行未知来源安全。
        """
        try:
            platform = self.platform(platform_id or SELF_PLATFORM_ID)
        except EmbedAccessError:
            platform = self.platform(SELF_PLATFORM_ID)
        return " ".join(platform.allowed_origins) or SELF_ORIGIN

    # ── 启动凭证 ─────────────────────────────────────────────────────────────

    def issue_launch(
        self,
        *,
        platform_id: str,
        subject: str,
        device: str,
        mode: str,
        console_session_id: str | None = None,
        grant_id: str | None = None,
    ) -> tuple[str, LaunchTicket]:
        """签发一次性启动凭证，返回 ``(token, ticket)``。

        ``console_session_id`` 只在「绑定一条已存在的会话」时给（例如只读观看别人的
        操作会话）；不给时由兑换方按模式决定是开新会话还是不开。

        ``grant_id`` 指向一条已经建好的授权记录（B 端 `POST /api/access/sessions` 的
        形态）：兑换时不再新建记录、不再开会话，只给那条记录签访问凭证。
        """
        platform = self.platform(platform_id)
        if not platform.allowed_origins:
            # §5.4：接入方允许来源为空时拒绝签发。
            raise EmbedAccessError(
                f"platform {platform.platform_id} has no allowed embed origin configured",
                status=409, code="embed_origin_not_configured",
            )
        device = str(device or "").strip()
        if not device:
            raise EmbedAccessError("device is required", status=422, code="validation")
        if mode not in MODES:
            raise EmbedAccessError("mode must be control or view", status=422, code="validation")
        now = self.clock()
        ticket = LaunchTicket(
            ticket_id=f"lnc-{secrets.token_hex(8)}",
            platform_id=platform.platform_id,
            subject=str(subject or "").strip() or "unknown",
            device=device,
            mode=mode,
            expires_at=now + self.launch_ttl_s,
            console_session_id=str(console_session_id or "").strip() or None,
            grant_id=str(grant_id or "").strip() or None,
        )
        token = secrets.token_urlsafe(32)
        with self._guard:
            self._purge(now)
            self._launches[token] = ticket
        logger.info(
            f"embed launch issued: ticket={ticket.ticket_id} platform={ticket.platform_id} "
            f"subject={ticket.subject} device={device} mode={mode}"
        )
        return token, ticket

    def peek_launch(self, token: str) -> LaunchTicket | None:
        """只读地看一眼启动凭证，不作废。

        `/embed/device-console` 用它按接入方算 ``frame-ancestors``：页面本体加载不该消耗
        一次性凭证，否则紧随其后的兑换必然失败。已兑换但未过期的凭证在这里仍可见，
        这样 iframe 重新加载时 CSP 头依旧正确。
        """
        value = str(token or "").strip()
        if not value:
            return None
        now = self.clock()
        with self._guard:
            self._purge(now)
            ticket = self._launches.get(value)
        if ticket is None or ticket.expires_at <= now:
            return None
        return ticket

    def redeem_launch(self, token: str) -> LaunchTicket:
        """兑换启动凭证：校验并当场作废（§5.2 的一次性只施加于这里）。"""
        value = str(token or "").strip()
        if not value:
            raise EmbedAccessError(
                "a launch token is required", status=401, code="launch_token_required",
            )
        now = self.clock()
        with self._guard:
            self._purge(now)
            ticket = self._launches.get(value)
            if ticket is None or ticket.expires_at <= now:
                raise EmbedAccessError(
                    "launch token is unknown or expired", status=401, code="launch_token_invalid",
                )
            if ticket.used_at is not None:
                raise EmbedAccessError(
                    "launch token has already been redeemed",
                    status=401, code="launch_token_used",
                )
            ticket.used_at = now
        return ticket

    # ── 会话访问凭证 ─────────────────────────────────────────────────────────

    def mint_access_token(self, grant_id: str) -> str:
        """给一条授权记录签一个指针令牌 ``<grant_id>.<HMAC>``。

        令牌本身不带授权信息：能不能用、还能用多久、是不是已被撤销，全部在校验时
        回表读。所以撤销立即生效，不需要令牌黑名单。
        """
        value = str(grant_id or "").strip()
        if not value:
            raise EmbedAccessError("grant_id is required", status=422, code="validation")
        signature = hmac.new(self._token_key, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{value}.{signature}"

    def issue_access(
        self,
        ticket: LaunchTicket,
        *,
        console_session_id: str,
        controller_token: str | None,
    ) -> tuple[str, AccessGrant]:
        """按已兑换的启动凭证签发会话访问凭证，返回 ``(token, grant)``。

        票据带 ``grant_id`` 时绑定那条已存在的授权记录（B 端路径）：只补上会话标识与
        ``controller_token``，不新建记录。不带时新建一条 `active` 记录
        （autophone 自己的看板路径，BL-103 的形态）。

        ``controller_token`` 存进授权记录，不进返回值的可公开视图，也不进日志。
        """
        now = self.clock()
        session_id = str(console_session_id or "") or None
        if ticket.grant_id:
            # B 端在签启动凭证前已创建并绑定授权。兑换只读取，不能把已撤销的
            # closed/closing 记录重新写成 active，也不能覆盖并发撤销的结果。
            grant = self.grants.get(ticket.grant_id)
            if (grant is None or grant.status != GRANT_ACTIVE or grant.expires_at <= now
                    or grant.platform_id != ticket.platform_id or grant.subject != ticket.subject
                    or grant.device != ticket.device or grant.mode != ticket.mode
                    or grant.console_session_id != session_id):
                raise EmbedAccessError(
                    "access grant is unavailable for launch exchange",
                    status=401, code="console_access_invalid",
                )
        else:
            grant = self.grants.create(GrantRecord(
                grant_id=new_grant_id(),
                request_id=f"embed-{ticket.ticket_id}",
                platform_id=ticket.platform_id,
                subject=ticket.subject,
                device=ticket.device,
                mode=ticket.mode,
                status=GRANT_ACTIVE,
                console_session_id=session_id,
                controller_token=controller_token,
                expires_at=now + self.access_ttl_s,
                created_at=now,
            ))
        logger.info(
            f"embed access granted: grant={grant.grant_id} platform={grant.platform_id} "
            f"subject={grant.subject} device={grant.device} mode={grant.mode} "
            f"session={grant.console_session_id or '-'}"
        )
        return self.mint_access_token(grant.grant_id), grant

    def issue_session_access(
        self,
        *,
        platform_id: str,
        subject: str,
        device: str,
        console_session_id: str,
        controller_token: str | None,
        request_id: str,
        mode: str = "control",
    ) -> tuple[str, AccessGrant]:
        """给一条**调用方刚开出来的** console 会话签会话访问凭证。

        与 `issue_access()` 的差别是不经启动凭证：创建口
        ``POST /api/devices/{device}/console`` 没有 iframe，也就没有嵌入来源要判，
        但它开出来的会话同样需要一份能驱动 ``event`` / ``heartbeat`` / ``stop`` 的凭证
        （§5.1：``controller_token`` 不出浏览器，动作准入只认会话访问凭证）。

        同一条 console 会话已经有开着的授权记录时**复用那一条**（创建口按
        ``request_id`` 幂等，重试拿回同一条会话，不该再签第二条记录）。没有时先清掉
        这台设备上的残留 `active` 记录——调用方此刻持有 lease，就证明那条记录背后的
        会话已经不在了，与 `close_open_grant_for_device()` 的用途一致。
        """
        session_id = str(console_session_id or "").strip()
        if not session_id:
            raise EmbedAccessError(
                "console_session_id is required", status=422, code="validation",
            )
        if mode not in MODES:
            raise EmbedAccessError("mode must be control or view", status=422, code="validation")
        platform = self.platform(platform_id)
        existing = self.grants.find(console_session_id=session_id)
        if existing is not None and existing.is_open():
            changes: dict[str, Any] = {"status": GRANT_ACTIVE}
            if controller_token is not None:
                changes["controller_token"] = controller_token
            grant = self.grants.update(existing.grant_id, **changes)
            return self.mint_access_token(grant.grant_id), grant
        self.close_open_grant_for_device(device, reason="session_gone")
        now = self.clock()
        grant = self.grants.create(GrantRecord(
            grant_id=new_grant_id(),
            request_id=str(request_id or "").strip() or f"console-{secrets.token_hex(8)}",
            platform_id=platform.platform_id,
            subject=str(subject or "").strip() or "unknown",
            device=str(device or "").strip(),
            mode=mode,
            status=GRANT_ACTIVE,
            console_session_id=session_id,
            controller_token=controller_token,
            expires_at=now + self.access_ttl_s,
            created_at=now,
        ))
        logger.info(
            f"console session access granted: grant={grant.grant_id} "
            f"platform={grant.platform_id} subject={grant.subject} device={grant.device} "
            f"session={session_id}"
        )
        return self.mint_access_token(grant.grant_id), grant

    def resolve_access(self, token: str) -> AccessGrant | None:
        """校验会话访问凭证；未知、签名不符、已过期或已撤销都返回 ``None``。"""
        grant_id = self._verify_access_token(token)
        if grant_id is None:
            return None
        grant = self.grants.get(grant_id)
        if grant is None or not grant.is_usable(self.clock()):
            return None
        return grant

    def peek_access(self, token: str) -> AccessGrant | None:
        """按凭证读授权记录，**不判**状态与过期。

        到期处理（§8.5）要知道「这条凭证指向哪条已过期的授权」才能触发关闭；
        `resolve_access()` 对过期返回 ``None``，拿不到记录也就无从关闭。
        """
        grant_id = self._verify_access_token(token)
        if grant_id is None:
            return None
        return self.grants.get(grant_id)

    def _verify_access_token(self, token: str) -> str | None:
        value = str(token or "").strip()
        grant_id, _, signature = value.partition(".")
        if not grant_id or not signature:
            return None
        expected = hmac.new(
            self._token_key, grant_id.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        return grant_id

    def revoke_access(self, token: str, *, reason: str = "revoked") -> bool:
        """撤销一条会话访问凭证（§7.2 第 5 条的 web 侧半步）。"""
        grant_id = self._verify_access_token(token)
        if grant_id is None:
            return False
        return self.close_grant(grant_id, reason=reason)

    def close_open_grant_for_device(self, device: str, *, reason: str) -> int:
        """把某台设备上还开着的 `active` **control** 授权推到 `closed`。

        用途只有一个：**调用方刚刚在这台设备上拿到 lease**（`console_start` 成功），
        这就证明先前那条 `active` 记录背后的会话已经不在了（心跳超时回收、rig 重启），
        表里那一行是残留。没有这一步，残留行会在 `uq_active_grant_per_device` 上永远
        挡住这台设备。

        **只清 `mode=control`**：`uq_active_grant_per_device` 自 2026-09-09 起只覆盖
        control 授权，view 授权既挡不住设备，也不是本方法要解决的问题；指向已消失
        会话的 view 授权由 `revoke_session(console_session_id)` 与到期兜底。

        **不动 `creating` 与 `closing`**：前者是另一路正在创建（结果未知，§8.2 禁止
        据此另分配），后者是清理未确认（§8.4 不得宣告设备可复用）。
        """
        key = str(device or "").strip()
        if not key:
            return 0
        stale = [
            row for row in self.grants.list(statuses=(GRANT_ACTIVE,), devices=[key])
            if row.mode == "control"
        ]
        for row in stale:
            self.grants.update(row.grant_id, status=GRANT_CLOSED, closed_reason=reason)
            logger.info(
                f"embed access reaped a stale grant: grant={row.grant_id} device={key} "
                f"reason={reason}"
            )
        return len(stale)

    def close_grant(self, grant_id: str, *, reason: str = "revoked") -> bool:
        """把一条授权记录推到 `closed`，凭证随之失效。"""
        grant = self.grants.get(grant_id)
        if grant is None or grant.status == GRANT_CLOSED:
            return False
        self.grants.update(grant_id, status=GRANT_CLOSED, closed_reason=reason)
        logger.info(f"embed access revoked: grant={grant_id} reason={reason}")
        return True

    def revoke_session(self, console_session_id: str, *, reason: str = "session_closed") -> int:
        """会话结束后连带作废指向它的全部凭证。

        实现是把这些授权记录推到 `closed`：凭证是指针，记录一关凭证立刻失效。
        """
        session_id = str(console_session_id or "").strip()
        if not session_id:
            return 0
        rows = [
            row for row in self.grants.list(statuses=GRANT_OPEN_STATUSES)
            if row.console_session_id == session_id
        ]
        for row in rows:
            self.grants.update(row.grant_id, status=GRANT_CLOSED, closed_reason=reason)
        if rows:
            logger.info(
                f"embed access revoked with the session: session={session_id} "
                f"count={len(rows)} reason={reason}"
            )
        return len(rows)

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _purge(self, now: float) -> None:
        """清掉过期的启动凭证。调用方已持锁。授权记录不在这里回收——它的终态由
        §八 的清理流程写，过期与否由 `is_usable()` 每次判定。"""
        for token, ticket in list(self._launches.items()):
            if ticket.expires_at <= now:
                self._launches.pop(token, None)

    # ── 配置装载 ─────────────────────────────────────────────────────────────

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> "EmbedAccessRegistry":
        """按环境变量装配（一期的最小接入方配置，BL-104 扩成完整配置文件）。

        - ``AUTOPHONE_EMBED_PLATFORMS_FILE``：接入方配置文件（JSON），五项键名见
          `load_platforms()`。BL-103 与 BL-104 共用**同一个环境变量与同一份文件**，
          BL-104 只加字段，旧文件照旧可读。设备池重叠时本方法抛 `AccessConfigError`。
        - ``AUTOPHONE_EMBED_ALLOWED_ORIGINS``：逗号分隔，追加到 autophone 自己的
          允许来源上（看板经反向代理换域名时用；``'self'`` 恒在）。
        - ``AUTOPHONE_EMBED_LAUNCH_TTL_S`` / ``AUTOPHONE_EMBED_ACCESS_TTL_S``：两种凭证的
          有效期，秒。
        - ``AUTOPHONE_EMBED_TOKEN_KEY``：会话访问凭证的签名密钥。不设则每进程随机，
          此时已签出的凭证不跨进程重启存活（授权记录本身仍在控制面表里）。
        """
        source = os.environ if env is None else env
        platforms: dict[str, AccessPlatform] = {}

        raw_path = str(source.get("AUTOPHONE_EMBED_PLATFORMS_FILE", "") or "").strip()
        if raw_path:
            platforms.update(_load_platforms_file(Path(raw_path)))

        extra = _normalize_origins(
            str(source.get("AUTOPHONE_EMBED_ALLOWED_ORIGINS", "") or "").split(","),
            platform_id=SELF_PLATFORM_ID,
        )
        if extra:
            existing = platforms.get(SELF_PLATFORM_ID)
            merged = tuple(existing.allowed_origins if existing else ()) + extra
            platforms[SELF_PLATFORM_ID] = (
                AccessPlatform(
                    SELF_PLATFORM_ID,
                    merged,
                    credential_hash=existing.credential_hash if existing else "",
                    device_pool=existing.device_pool if existing else (),
                    shared_account=existing.shared_account if existing else False,
                )
            )

        raw_key = str(source.get("AUTOPHONE_EMBED_TOKEN_KEY", "") or "").strip()
        return cls(
            platforms,
            clock=clock,
            launch_ttl_s=_float_env(source, "AUTOPHONE_EMBED_LAUNCH_TTL_S", DEFAULT_LAUNCH_TTL_S),
            access_ttl_s=_float_env(source, "AUTOPHONE_EMBED_ACCESS_TTL_S", DEFAULT_ACCESS_TTL_S),
            token_key=raw_key.encode("utf-8") if raw_key else None,
        )


def _float_env(source: Mapping[str, str], key: str, default: float) -> float:
    raw = str(source.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"embed access: {key}={raw!r} is not a number; using {default}")
        return default
    if value <= 0:
        logger.warning(f"embed access: {key}={raw!r} must be positive; using {default}")
        return default
    return value


def load_platforms(data: Mapping[str, Any]) -> dict[str, AccessPlatform]:
    """把接入方配置（已解析的映射）转成 `AccessPlatform` 表（§6.1 的五项）。

    一份配置形如：

    ```json
    {
      "acme": {
        "allowed_origins": ["https://acme.example.com"],
        "credential_hash": "sha256:2c26b46b...",
        "device_pool": ["vm"],
        "shared_account": false
      }
    }
    ```

    ``allowed_origins`` 是 BL-103 已有的键，BL-104 只加后三项，所以旧配置照旧可读：
    没写 ``credential_hash`` 的接入方不能调 B 端接口，没写 ``device_pool`` 的接入方
    没有可分配设备。**设备池重叠即抛 `AccessConfigError`**，不静默取其一。
    """
    platforms: dict[str, AccessPlatform] = {}
    for platform_id, raw in dict(data).items():
        key = str(platform_id or "").strip()
        if not key:
            continue
        entry = raw if isinstance(raw, Mapping) else {}
        origins = entry.get("allowed_origins") or []
        if isinstance(origins, str):
            origins = origins.split(",")
        pool = entry.get("device_pool") or []
        if isinstance(pool, str):
            pool = pool.split(",")
        platforms[key] = AccessPlatform(
            key,
            _normalize_origins(origins, platform_id=key),
            credential_hash=_normalize_credential_hash(entry.get("credential_hash"), platform_id=key),
            device_pool=_normalize_pool(pool),
            shared_account=bool(entry.get("shared_account", False)),
        )
    _assert_pools_disjoint(platforms)
    return platforms


def _normalize_pool(raw_pool: Iterable[str]) -> tuple[str, ...]:
    """设备池：去空、去重，保留登记顺序（分配算法按候选顺序尝试，§6.2 步 3）。"""
    devices: list[str] = []
    for raw in raw_pool:
        device = str(raw or "").strip()
        if device and device not in devices:
            devices.append(device)
    return tuple(devices)


def _normalize_credential_hash(raw: Any, *, platform_id: str) -> str:
    """凭证哈希：接受 ``sha256:<hex>`` 或裸 hex，统一成小写裸 hex。

    认不出的取值一律丢弃并告警，**不**当成有效哈希——留着会让一个写错的配置项
    表现为「凭证怎么都对不上」而不是「这个接入方没配凭证」。
    """
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        logger.warning(
            f"embed access: dropped an invalid credential_hash for platform={platform_id} "
            "(expected a sha256 hex digest)"
        )
        return ""
    return value


def _assert_pools_disjoint(platforms: Mapping[str, AccessPlatform]) -> None:
    """设备池按接入方互斥（§6.1）：重叠即拒绝加载。

    为什么是抛错而不是丢弃其中一个：两个接入方声称同一台设备时，谁该拿到它是**配置
    的意图问题**，程序替它选一个就把隔离破坏得无声无息。
    """
    owner: dict[str, str] = {}
    for platform_id in sorted(platforms):
        for device in platforms[platform_id].device_pool:
            previous = owner.get(device)
            if previous is not None and previous != platform_id:
                raise AccessConfigError(
                    f"device pools overlap on {device!r}: "
                    f"registered by both {previous!r} and {platform_id!r}"
                )
            owner[device] = platform_id


def _load_platforms_file(path: Path) -> dict[str, AccessPlatform]:
    """读接入方配置文件。

    读不出内容（文件不在、JSON 不合法、顶层不是对象）时告警并返回空表：结果是
    「一个接入方都没有」，本身就是 fail-closed。**设备池重叠不同**——它读得出内容
    但内容会破坏隔离，所以 `load_platforms()` 抛出的 `AccessConfigError` 一路上抛，
    进程起不来即是「拒绝加载」（§6.1）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(f"embed access: platform config not found: {path}")
        return {}
    except (OSError, ValueError) as error:
        logger.warning(f"embed access: could not read the platform config {path}: {error}")
        return {}
    if not isinstance(data, Mapping):
        logger.warning(f"embed access: platform config {path} must be a JSON object")
        return {}
    return load_platforms(data)
