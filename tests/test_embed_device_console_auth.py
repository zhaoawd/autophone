"""嵌入页会话访问凭证鉴权的替身测试（BL-103）。

对应 docs/platform-device-console-embed.md §五与 §十二 步 4 的完成判据：

1. 无凭证读 frame 得 401，且这条在**未设 `AUTOPHONE_AUTH`** 时同样成立
   （不被 `_basic_auth` 的开发模式放行分支跳过），同时不影响其余路由的开发模式放行；
2. 凭证只能访问它绑定的设备与会话；view 模式凭证调 event / stop 得 403；
3. 启动凭证一次性，用后再兑换即拒；
4. `controller_token` 不出浏览器：兑换与五条会话路由的响应体里都没有它，
   服务端自己从授权记录取了交给 rig；
5. `/embed/device-console` 按凭证所属接入方下发 `frame-ancestors`，
   来源列表为空的接入方拒绝签发启动凭证。

设备一侧全部用替身：`_device_sessions` 被换成一个只记调用的假操作面，测试不碰真 VM。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from autophone.application.embed_access import (
    AccessPlatform,
    EmbedAccessError,
    EmbedAccessRegistry,
)
from autophone.adapters.http.app import dependencies as api


CONTROLLER_TOKEN = "controller-token-must-stay-server-side"
JPEG = b"\xff\xd8jpeg-bytes"


class FakeSessions:
    """交互通道操作面的替身：记下每次调用，返回形状与真面一致的最小结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.busy = False

    def call(self, device: str, op: str, payload: dict) -> dict:
        self.calls.append((device, op, dict(payload)))
        if op == "console_start":
            if self.busy:
                from autophone.application.device_session import DeviceSessionRefused

                raise DeviceSessionRefused(
                    "device is busy: run R1", status=409, code="device_busy",
                )
            return {
                "session_id": "ctl-1",
                "device": device,
                "controller_token": CONTROLLER_TOKEN,
                "state": "live",
                "lifecycle": "active",
                "started_at": 1000.0,
                "geometry": {"preview": [430, 932]},
            }
        if op == "console_device_status":
            return {"device": device, "active": True, "session_id": "ctl-9", "started_at": 900.0}
        if op == "console_view":
            return {"session_id": payload["session_id"], "device": device, "state": "live", "seq": 3}
        if op == "console_frame":
            return {
                "jpeg_b64": base64.b64encode(JPEG).decode("ascii"),
                "seq": 3,
                "captured_at": 1000.0,
            }
        if op in ("console_event", "console_heartbeat", "console_stop"):
            # 与 `DeviceConsoleManager._check_token()` 同判据：动作三条要 rig 侧的
            # 会话 token，缺了或不对都是 409 `token_mismatch`。
            if payload.get("controller_token") != CONTROLLER_TOKEN:
                from autophone.application.device_session import DeviceSessionRefused

                raise DeviceSessionRefused(
                    "controller token mismatch", status=409, code="token_mismatch",
                )
        if op == "console_stop":
            return {"ok": True, "reason": "stopped"}
        return {"ok": True, "event_id": payload.get("event", {}).get("event_id", "")}

    def token_of(self, op: str) -> str | None:
        for _device, name, payload in self.calls:
            if name == op:
                return payload.get("controller_token")
        return None


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> FakeSessions:
    fake = FakeSessions()
    monkeypatch.setattr(api, "_device_sessions", lambda device: fake)
    return fake


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> EmbedAccessRegistry:
    """每个用例一份干净的凭证登记表，外加一个 B 端接入方与一个没登记来源的接入方。"""
    fresh = EmbedAccessRegistry({
        "acme": AccessPlatform("acme", ("https://acme.example.com",)),
        "noorigin": AccessPlatform("noorigin", ()),
    })
    monkeypatch.setattr(api, "embed_access", fresh)
    return fresh


@pytest.fixture
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设 `AUTOPHONE_AUTH`：中间件对普通路由放行、actor 记 `local`。"""
    monkeypatch.setattr(api, "_BASIC_AUTH_USERS", {})


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


def _launch(client: TestClient, **body) -> str:
    res = client.post("/api/access/launch-tokens", json=body)
    assert res.status_code == 200, res.text
    return res.json()["launch_token"]


def _exchange(client: TestClient, launch_token: str) -> dict:
    res = client.post("/api/access/embed-sessions", json={"launch_token": launch_token})
    assert res.status_code == 200, res.text
    return res.json()


def _session(client: TestClient, device: str = "rig", mode: str = "control", **extra) -> dict:
    return _exchange(client, _launch(client, device=device, mode=mode, **extra))


# ── 判据 1：开发模式下会话授权仍成立 ─────────────────────────────────────────

def test_frame_without_a_credential_is_401_even_with_auth_disabled(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    assert api._BASIC_AUTH_USERS == {}, "本用例要的就是开发模式"

    for path in (
        "/api/devices/rig/console/ctl-1",
        "/api/devices/rig/console/ctl-1/frame",
        "/api/devices/rig/console/ctl-1/event",
        "/api/devices/rig/console/ctl-1/heartbeat",
        "/api/devices/rig/console/ctl-1/stop",
    ):
        res = client.get(path) if path.endswith(("ctl-1", "frame")) else client.post(path, json={})
        assert res.status_code == 401, f"{path} 在开发模式下被放行了：{res.status_code}"
        assert res.json()["detail"]["code"] == "console_access_required"
    # 一条都没打到设备。
    assert sessions.calls == []


def test_auth_disabled_still_lets_the_rest_of_the_dashboard_through(
    client: TestClient, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    """会话闸只收在 console 会话路由上，别的路由的开发模式放行不受影响。"""
    assert client.get("/api/version").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    # 嵌入页本体不要凭证（页面里没有数据），CSP 头才是它的防线。
    embed = client.get("/embed/device-console", params={"device": "rig", "mode": "control"})
    assert embed.status_code == 200


# ── 判据 2：凭证的绑定与模式 ─────────────────────────────────────────────────

def test_a_session_credential_reads_the_frame_it_is_bound_to(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    session = _session(client)
    headers = {"X-Console-Access-Token": session["access_token"]}

    frame = client.get("/api/devices/rig/console/ctl-1/frame", headers=headers)
    assert frame.status_code == 200
    assert frame.content == JPEG
    view = client.get("/api/devices/rig/console/ctl-1", headers=headers)
    assert view.status_code == 200 and view.json()["state"] == "live"


def test_a_credential_cannot_reach_another_device_or_session(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    session = _session(client)
    headers = {"X-Console-Access-Token": session["access_token"]}

    other_device = client.get("/api/devices/other/console/ctl-1/frame", headers=headers)
    assert other_device.status_code == 403
    assert other_device.json()["detail"]["code"] == "console_access_device_mismatch"

    other_session = client.get("/api/devices/rig/console/ctl-2/frame", headers=headers)
    assert other_session.status_code == 403
    assert other_session.json()["detail"]["code"] == "console_access_session_mismatch"


def test_an_unknown_or_revoked_credential_is_401(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    session = _session(client)
    bad = client.get(
        "/api/devices/rig/console/ctl-1/frame",
        headers={"X-Console-Access-Token": "not-a-real-token"},
    )
    assert bad.status_code == 401 and bad.json()["detail"]["code"] == "console_access_invalid"

    # 会话结束即连带作废凭证：同一条凭证再读画面就是 401，而不是继续可用。
    stop = client.post(
        "/api/devices/rig/console/ctl-1/stop",
        headers={"X-Console-Access-Token": session["access_token"]},
    )
    assert stop.status_code == 200
    after = client.get(
        "/api/devices/rig/console/ctl-1/frame",
        headers={"X-Console-Access-Token": session["access_token"]},
    )
    assert after.status_code == 401


def test_a_view_credential_may_read_but_not_act(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    """view 模式凭证：view / frame 放行，event / heartbeat / stop 一律 403（§5.1）。"""
    session = _session(client, mode="view", session_id="ctl-1")
    headers = {"X-Console-Access-Token": session["access_token"]}
    # view 模式绑定既有会话，不另开一条：兑换过程没有 console_start。
    assert [call[1] for call in sessions.calls] == []

    assert client.get("/api/devices/rig/console/ctl-1/frame", headers=headers).status_code == 200
    assert client.get("/api/devices/rig/console/ctl-1", headers=headers).status_code == 200
    for action in ("event", "heartbeat", "stop"):
        res = client.post(f"/api/devices/rig/console/ctl-1/{action}", headers=headers, json={})
        assert res.status_code == 403, action
        assert res.json()["detail"]["code"] == "console_access_read_only"
    assert [call[1] for call in sessions.calls if call[1] in ("console_event", "console_stop")] == []


def test_a_session_credential_cannot_open_a_new_session(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭证不能调创建入口（§5.1）：配了 Basic Auth 时它在创建口上不算身份。"""
    monkeypatch.setattr(api, "_BASIC_AUTH_USERS", {})
    session = _session(client)
    monkeypatch.setattr(api, "_BASIC_AUTH_USERS", {"u": "p"})

    res = client.post(
        "/api/devices/rig/console",
        headers={"X-Console-Access-Token": session["access_token"]},
        json={},
    )
    assert res.status_code == 401


def test_basic_auth_is_still_accepted_on_the_session_routes(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两种身份并列：配了 Basic Auth 时，看板用户不带会话凭证也能读会话路由。"""
    monkeypatch.setattr(api, "_BASIC_AUTH_USERS", {"u": "p"})

    assert client.get("/api/devices/rig/console/ctl-1/frame").status_code == 401
    ok = client.get("/api/devices/rig/console/ctl-1/frame", auth=("u", "p"))
    assert ok.status_code == 200 and ok.content == JPEG


# ── 判据 3：启动凭证一次性 ───────────────────────────────────────────────────

def test_a_launch_token_can_only_be_redeemed_once(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    token = _launch(client, device="rig", mode="control")
    assert _exchange(client, token)["session_id"] == "ctl-1"

    again = client.post("/api/access/embed-sessions", json={"launch_token": token})
    assert again.status_code == 401
    assert again.json()["detail"]["code"] == "launch_token_used"

    missing = client.post("/api/access/embed-sessions", json={})
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "launch_token_required"

    unknown = client.post("/api/access/embed-sessions", json={"launch_token": "nope"})
    assert unknown.status_code == 401
    assert unknown.json()["detail"]["code"] == "launch_token_invalid"


def test_an_expired_launch_token_is_refused(registry: EmbedAccessRegistry) -> None:
    now = [1000.0]
    aging = EmbedAccessRegistry(clock=lambda: now[0], launch_ttl_s=60.0)
    token, _ticket = aging.issue_launch(
        platform_id="autophone", subject="u", device="rig", mode="control",
    )
    now[0] += 61.0
    with pytest.raises(EmbedAccessError) as error:
        aging.redeem_launch(token)
    assert error.value.code == "launch_token_invalid"
    assert aging.peek_launch(token) is None


def test_the_exchange_reports_device_busy_with_the_holder(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    """被占用时兑换失败，持有者由服务端补齐（页面此刻还没有凭证，问不了设备状态）。"""
    sessions.busy = True
    res = client.post(
        "/api/access/embed-sessions",
        json={"launch_token": _launch(client, device="rig", mode="control")},
    )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "device_busy"
    assert detail["holder"]["owner_kind"] == "web_control"
    assert detail["holder"]["session_id"] == "ctl-9"


# ── 判据 4：controller_token 不出浏览器 ──────────────────────────────────────

def test_the_controller_token_never_reaches_the_browser(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    exchange = client.post(
        "/api/access/embed-sessions",
        json={"launch_token": _launch(client, device="rig", mode="control")},
    )
    assert exchange.status_code == 200
    assert CONTROLLER_TOKEN not in exchange.text
    assert "controller_token" not in exchange.json()

    headers = {"X-Console-Access-Token": exchange.json()["access_token"]}
    bodies = [
        client.get("/api/devices/rig/console/ctl-1", headers=headers),
        client.get("/api/devices/rig/console/ctl-1/frame", headers=headers),
        client.post("/api/devices/rig/console/ctl-1/event", headers=headers,
                    json={"event_id": "e1", "kind": "tap", "px": 1, "py": 2}),
        client.post("/api/devices/rig/console/ctl-1/heartbeat", headers=headers),
        client.post("/api/devices/rig/console/ctl-1/stop", headers=headers),
    ]
    for response in bodies:
        assert response.status_code == 200, response.text
        assert CONTROLLER_TOKEN not in response.text
        assert "controller_token" not in response.text

    # 服务端自己取到了 token 并交给 rig：三条动作路由都带上了它。
    for op in ("console_event", "console_heartbeat", "console_stop"):
        assert sessions.token_of(op) == CONTROLLER_TOKEN, op


def test_a_client_supplied_controller_token_is_ignored_on_the_credential_path(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    """凭证路径不看 `X-Controller-Token`：token 只能来自服务端的授权记录。"""
    session = _session(client)
    res = client.post(
        "/api/devices/rig/console/ctl-1/heartbeat",
        headers={
            "X-Console-Access-Token": session["access_token"],
            "X-Controller-Token": "smuggled",
        },
    )
    assert res.status_code == 200
    assert sessions.token_of("console_heartbeat") == CONTROLLER_TOKEN


def test_the_embed_page_javascript_has_no_controller_token() -> None:
    """嵌入页的代码行里没有 `controller_token`（注释里说明去向不算）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "autophone/adapters/http/static/embed/device-console.js").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//"))
    )
    assert "controller_token" not in code.lower()
    assert "X-Controller-Token" not in code
    assert "X-Console-Access-Token" in code


# ── 判据 5：frame-ancestors 与允许来源 ───────────────────────────────────────

def test_the_embed_page_sends_frame_ancestors_for_the_platform_of_the_launch_token(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    plain = client.get("/embed/device-console", params={"device": "rig", "mode": "control"})
    assert plain.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert plain.headers["cache-control"] == "no-store"
    assert plain.headers["referrer-policy"] == "no-referrer"

    token = _launch(client, device="rig", mode="control", platform="acme")
    embedded = client.get("/embed/device-console", params={
        "device": "rig", "mode": "control", "launch": token,
    })
    assert embedded.headers["content-security-policy"] == (
        "frame-ancestors https://acme.example.com"
    )
    # 页面加载不消耗一次性凭证：紧随其后的兑换仍然成立。
    assert client.post(
        "/api/access/embed-sessions", json={"launch_token": token},
    ).status_code == 200


def test_frame_ancestors_falls_back_to_self_for_an_unusable_launch_token(
    client: TestClient, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    res = client.get("/embed/device-console", params={
        "device": "rig", "mode": "control", "launch": "expired-or-forged",
    })
    assert res.headers["content-security-policy"] == "frame-ancestors 'self'"


def test_a_platform_without_allowed_origins_cannot_get_a_launch_token(
    client: TestClient, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    res = client.post("/api/access/launch-tokens", json={
        "device": "rig", "mode": "control", "platform": "noorigin",
    })
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "embed_origin_not_configured"

    unknown = client.post("/api/access/launch-tokens", json={
        "device": "rig", "mode": "control", "platform": "who",
    })
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "embed_platform_unknown"


def test_launch_token_validates_its_input(
    client: TestClient, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    assert client.post("/api/access/launch-tokens", json={"mode": "control"}).status_code == 422
    assert client.post(
        "/api/access/launch-tokens", json={"device": "rig", "mode": "admin"},
    ).status_code == 422


# ── 接入方配置的最小形态 ─────────────────────────────────────────────────────

def test_the_registry_loads_platforms_and_extra_origins_from_the_environment(tmp_path) -> None:
    config = tmp_path / "platforms.json"
    config.write_text(
        '{"acme": {"allowed_origins": ["https://acme.example.com/", "javascript:alert(1)"]}}',
        encoding="utf-8",
    )
    built = EmbedAccessRegistry.from_environment({
        "AUTOPHONE_EMBED_PLATFORMS_FILE": str(config),
        "AUTOPHONE_EMBED_ALLOWED_ORIGINS": "https://autophone.example:8443, ",
        "AUTOPHONE_EMBED_LAUNCH_TTL_S": "45",
    })

    # 非 http(s) 的来源被丢掉，不进 CSP。
    assert built.frame_ancestors("acme") == "https://acme.example.com"
    # autophone 自己恒含 'self'，环境变量里的来源追加在后面。
    assert built.frame_ancestors() == "'self' https://autophone.example:8443"
    assert built.launch_ttl_s == 45.0


def test_the_actor_of_a_credential_names_the_platform_and_the_user(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    """actor 写成 `platform:<platform_id>/<user>`（§5.1）。"""
    _token, ticket = registry.issue_launch(
        platform_id="acme", subject="zhang", device="rig", mode="control",
    )
    _access, grant = registry.issue_access(
        ticket, console_session_id="ctl-1", controller_token=CONTROLLER_TOKEN,
    )
    assert grant.actor() == "platform:acme/zhang"
    assert "controller_token" not in grant.public()


# ── 鉴权分层矩阵（§5.1 / §5.7） ──────────────────────────────────────────────
#
# 修复前的两条互斥取值（2026-09-09 rig 通道活机补测，证据
# `evidence/outputs/device-console-rig-channel-20260909/`）：
#
# 1. 配了 `AUTOPHONE_AUTH` 时，只带 `X-Console-Access-Token` 的 console 会话路由被
#    外层 `_basic_auth` 拒成纯文本 401 + `WWW-Authenticate: Basic`，内层的会话闸
#    根本没跑到——配了 Basic Auth 的部署上嵌入页取不到帧、发不了动作；
# 2. 未配 `AUTOPHONE_AUTH` 时，创建口开出来的会话拿不到任何可用凭证，`stop` 恒 401
#    `console_access_required`，设备被占到心跳超时。
#
# 下面按 {配置 Basic Auth, 开发模式} × {有效凭证, 无效凭证, 只带 Basic Auth, 什么都不带}
# 固定 `frame` 与 `stop` 两条路由的返回码与 code。

BASIC = ("u", "p")


@pytest.fixture
def basic_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置了 `AUTOPHONE_AUTH`。"""
    monkeypatch.setattr(api, "_BASIC_AUTH_USERS", {BASIC[0]: BASIC[1]})


def _create_console(client: TestClient, device: str = "rig", **kwargs) -> dict:
    """走看板创建口 `POST /api/devices/{device}/console` 开一条会话。"""
    res = client.post(f"/api/devices/{device}/console", json={}, **kwargs)
    assert res.status_code == 200, res.text
    return res.json()


def _console_credentials(kind: str, session: dict) -> tuple[dict, dict]:
    """把矩阵的一格翻译成 `(headers, kwargs)`。"""
    if kind == "valid":
        return {"X-Console-Access-Token": session["access_token"]}, {}
    if kind == "invalid":
        return {"X-Console-Access-Token": "grt-nope.deadbeef"}, {}
    if kind == "basic-only":
        return {}, {"auth": BASIC}
    return {}, {}


def _assert_console_response(res, status: int, code: str | None, *, label: str) -> None:
    """一格的判据：返回码 + code，外加「是不是 Basic 挑战」这一条。"""
    assert res.status_code == status, f"{label}: {res.status_code} {res.text}"
    challenge = res.headers.get("www-authenticate", "")
    if code is None:
        assert not challenge, label
        return
    if code == "basic":
        # 什么都不带才回 Basic 挑战：浏览器该弹框的那一格没变。
        assert challenge.startswith("Basic"), label
        return
    # 其余拒绝都是结构化 JSON，不带挑战头：跨域 iframe 不会被弹框劫持。
    assert not challenge, label
    assert res.json()["detail"]["code"] == code, label


@pytest.mark.parametrize(
    ("credential", "frame", "stop"),
    [
        ("valid", (200, None), (200, None)),
        ("invalid", (401, "console_access_invalid"), (401, "console_access_invalid")),
        # 只带 Basic Auth：中间件放行（看板用户是身份），但动作三条还要 rig 侧的会话
        # token，而它只在授权记录里，所以 `stop` 停在 rig 层的 409 而不是 401。
        ("basic-only", (200, None), (409, "token_mismatch")),
        ("none", (401, "basic"), (401, "basic")),
    ],
)
def test_the_auth_matrix_with_basic_auth_configured(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    basic_auth_mode: None, credential: str, frame: tuple, stop: tuple,
) -> None:
    """配了 Basic Auth：会话凭证与 Basic Auth 并列可用，坏凭证不再退回 Basic 挑战。"""
    session = _create_console(client, auth=BASIC)
    headers, kwargs = _console_credentials(credential, session)

    got_frame = client.get("/api/devices/rig/console/ctl-1/frame", headers=headers, **kwargs)
    _assert_console_response(got_frame, *frame, label=f"{credential}/frame")
    if frame[1] is None:
        assert got_frame.content == JPEG

    got_stop = client.post("/api/devices/rig/console/ctl-1/stop", headers=headers, **kwargs)
    _assert_console_response(got_stop, *stop, label=f"{credential}/stop")
    if stop[1] is None:
        assert got_stop.json()["ok"] is True


@pytest.mark.parametrize(
    ("credential", "frame", "stop"),
    [
        ("valid", (200, None), (200, None)),
        ("invalid", (401, "console_access_invalid"), (401, "console_access_invalid")),
        # 开发模式下 Basic 凭据不构成身份（§5.1），所以这一格与「什么都不带」同判。
        ("basic-only", (401, "console_access_required"), (401, "console_access_required")),
        ("none", (401, "console_access_required"), (401, "console_access_required")),
    ],
)
def test_the_auth_matrix_in_development_mode(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    dev_mode: None, credential: str, frame: tuple, stop: tuple,
) -> None:
    """开发模式：会话授权照样成立（§5.1），创建口签的凭证是唯一可用身份。"""
    session = _create_console(client)
    headers, kwargs = _console_credentials(credential, session)

    got_frame = client.get("/api/devices/rig/console/ctl-1/frame", headers=headers, **kwargs)
    _assert_console_response(got_frame, *frame, label=f"{credential}/frame")
    if frame[1] is None:
        assert got_frame.content == JPEG

    got_stop = client.post("/api/devices/rig/console/ctl-1/stop", headers=headers, **kwargs)
    _assert_console_response(got_stop, *stop, label=f"{credential}/stop")
    if stop[1] is None:
        assert got_stop.json()["ok"] is True


def test_a_basic_auth_deployment_lets_a_session_credential_through(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    basic_auth_mode: None,
) -> None:
    """修复点 1：嵌入路径开的会话在配了 Basic Auth 的部署上也能取帧、发动作。"""
    # 签启动凭证的是宿主（看板用户，带 Basic Auth）；兑换与之后的会话路由都由跨域
    # iframe 发起，一份 Basic 凭据都带不了。
    launch = client.post(
        "/api/access/launch-tokens",
        json={"device": "rig", "mode": "control", "platform": "acme"},
        auth=BASIC,
    )
    assert launch.status_code == 200, launch.text
    exchange = _exchange(client, launch.json()["launch_token"])
    headers = {"X-Console-Access-Token": exchange["access_token"]}

    assert client.get("/api/devices/rig/console/ctl-1/frame", headers=headers).status_code == 200
    event = client.post(
        "/api/devices/rig/console/ctl-1/event", headers=headers,
        json={"event_id": "e1", "kind": "tap", "px": 1, "py": 2},
    )
    assert event.status_code == 200
    assert client.post(
        "/api/devices/rig/console/ctl-1/heartbeat", headers=headers,
    ).status_code == 200
    assert sessions.token_of("console_event") == CONTROLLER_TOKEN


@pytest.mark.parametrize("auth_mode", ["basic", "dev"])
def test_the_create_route_hands_back_a_usable_session_credential(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    monkeypatch: pytest.MonkeyPatch, auth_mode: str,
) -> None:
    """修复点 2：两种模式下创建口开的会话都能用它返回的凭证结束掉。"""
    monkeypatch.setattr(
        api, "_BASIC_AUTH_USERS", {BASIC[0]: BASIC[1]} if auth_mode == "basic" else {},
    )
    kwargs = {"auth": BASIC} if auth_mode == "basic" else {}
    session = _create_console(client, **kwargs)

    assert session["access_header"] == "x-console-access-token"
    assert session["session_id"] == "ctl-1"
    headers = {"X-Console-Access-Token": session["access_token"]}
    stop = client.post("/api/devices/rig/console/ctl-1/stop", headers=headers)
    assert stop.status_code == 200 and stop.json()["ok"] is True
    # 服务端自己把 rig 要的那把 token 取出来交了过去。
    assert sessions.token_of("console_stop") == CONTROLLER_TOKEN
    # 会话结束即作废凭证：同一条凭证再用就是 401。
    again = client.get("/api/devices/rig/console/ctl-1/frame", headers=headers)
    assert again.status_code == 401


@pytest.mark.parametrize("auth_mode", ["basic", "dev"])
def test_the_create_route_never_returns_the_controller_token(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    monkeypatch: pytest.MonkeyPatch, auth_mode: str,
) -> None:
    """`controller_token` 不出浏览器（§5.1），创建口也一样。"""
    monkeypatch.setattr(
        api, "_BASIC_AUTH_USERS", {BASIC[0]: BASIC[1]} if auth_mode == "basic" else {},
    )
    kwargs = {"auth": BASIC} if auth_mode == "basic" else {}
    res = client.post("/api/devices/rig/console", json={}, **kwargs)

    assert res.status_code == 200
    assert "controller_token" not in res.text
    assert CONTROLLER_TOKEN not in res.text


def test_the_create_route_is_idempotent_on_the_grant_too(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    """同一个 `request_id` 重试拿回同一条会话，也拿回同一条授权记录，不签第二条。"""
    first = client.post("/api/devices/rig/console", json={"request_id": "req-1"})
    second = client.post("/api/devices/rig/console", json={"request_id": "req-1"})

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["grant_id"] == second.json()["grant_id"]
    assert len(registry.grants.list(statuses=("active",))) == 1


def test_the_create_route_still_requires_basic_auth_when_configured(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    basic_auth_mode: None,
) -> None:
    """会话凭证不能创建会话（§5.1），创建口的豁免没有被这次修复扩大。"""
    session = _create_console(client, auth=BASIC)
    res = client.post(
        "/api/devices/rig/console",
        headers={"X-Console-Access-Token": session["access_token"]},
        json={},
    )
    assert res.status_code == 401
    assert res.headers.get("www-authenticate", "").startswith("Basic")


def test_the_credential_exemption_only_covers_console_session_routes(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry,
    basic_auth_mode: None,
) -> None:
    """带上会话凭证不能当万能钥匙：别的路由仍在 Basic Auth 之后。"""
    session = _create_console(client, auth=BASIC)
    headers = {"X-Console-Access-Token": session["access_token"]}

    for path in ("/api/version", "/api/devices/rig/console", "/api/access/launch-tokens"):
        res = client.get(path, headers=headers)
        assert res.status_code == 401, path
        assert res.headers.get("www-authenticate", "").startswith("Basic"), path


@pytest.mark.parametrize('path', ['/embed/device-console?device=vm-1&mode=control', '/static/embed/device-console.js'])
def test_embed_shell_loads_without_dashboard_credentials(client, monkeypatch, registry, path):
    monkeypatch.setattr(api, '_BASIC_AUTH_USERS', {'admin': 'secret'})
    response = client.get(path)
    assert response.status_code == 200
    assert 'www-authenticate' not in response.headers
    if path.startswith('/embed/'):
        assert response.headers['content-security-policy'] == "frame-ancestors 'self'"


@pytest.mark.parametrize('path', ['/', '/static/dashboard/core.js', '/static/embed/device-console.html', '/api/access/launch-tokens'])
def test_embed_shell_exemption_does_not_expose_other_routes(client, monkeypatch, path):
    monkeypatch.setattr(api, '_BASIC_AUTH_USERS', {'admin': 'secret'})
    assert client.get(path).status_code == 401


def test_old_demo_token_cannot_start_or_control_a_session(
    client: TestClient, sessions: FakeSessions, registry: EmbedAccessRegistry, dev_mode: None,
) -> None:
    _session(client)
    before = list(sessions.calls)
    old_token = "demo-access"
    exchange = client.post("/api/access/embed-sessions", json={"launch_token": old_token})
    assert exchange.status_code == 401
    assert exchange.json()["detail"]["code"] == "launch_token_invalid"
    for action in ("frame", "event", "heartbeat", "stop"):
        response = client.request(
            "GET" if action == "frame" else "POST",
            f"/api/devices/rig/console/ctl-1/{action}?demo=1",
            headers={"X-Console-Access-Token": old_token},
        )
        assert response.status_code == 401, action
        assert response.json()["detail"]["code"] == "console_access_invalid"
    assert sessions.calls == before
