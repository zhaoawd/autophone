"""ScreenLock and Control Plane coordination for vphone_mcp.

Standalone (stdlib-only) coordination module compatible with Autophone's
``autophone/device/lock.py`` file/quarantine semantics and Control Plane HTTP
endpoints (``autophone/control/routes.py``, ``autophone/control/device_session.py``,
``autophone/adapters/http/devices.py``).

Coordination modes (auto-detected in priority order):
1. ``managed_proxy``: ``AUTOPHONE_MANAGED_DEVICE_AGENT=1`` (parent process already
   holds ScreenLock + StageGuard and proxies UDS through AgentDeviceProxy).
2. ``control_plane``: any Control Plane endpoint or DSN env is configured
   (``AUTOPHONE_CONTROL_PLANE_URL``, ``AUTOPHONE_CONTROL_HTTP``,
   ``AUTOPHONE_API_URL``, ``AUTOPHONE_CONTROL_WS``, or ``AUTOPHONE_DATABASE_URL``).
   Always fails closed if unreachable or if the device is busy/quarantined; never
   silently falls back to raw UDS writes. Also layers local ScreenLock if a lock
   directory is configured on the same host.
3. ``local_lock``: local lock directory (``VPHONE_MCP_LOCK_DIR``,
   ``AUTOPHONE_LOCK_DIR``, or ``AUTOPHONE_STATE_DIR/runs/.locks``) + device key
   (``VPHONE_DEVICE``, ``AUTOPHONE_DEVICE``, ``AUTOPHONE_RIG_ID``, or ``AP_DEVICE``).
4. ``standalone``: backward-compatible direct ``VPHONE_SOCK`` mode when neither
   Control Plane nor local lock is configured. Refuses mutating operations with
   ``LockRequiredError`` when ``VPHONE_MCP_REQUIRE_LOCK=1``.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterator, Mapping
import urllib.error
from urllib.parse import quote, urlsplit, urlunsplit
import urllib.request
import uuid

logger = logging.getLogger(__name__)

# Matches autophone/device/lock.py's safe filename device segment rule.
_DEVICE_NAME_RE = re.compile(r"[A-Za-z0-9._:-]+")
_TRUTHY = frozenset({"1", "true", "yes", "on"})

DEFAULT_LOCK_TTL_S = 30.0
MIN_LOCK_TTL_S = 1.0
MAX_LOCK_TTL_S = 300.0
DEFAULT_HTTP_TIMEOUT_S = 5.0
DEFAULT_CONTROL_PLANE_URL = "http://127.0.0.1:8100"

MODE_MANAGED_PROXY = "managed_proxy"
MODE_CONTROL_PLANE = "control_plane"
MODE_LOCAL_LOCK = "local_lock"
MODE_STANDALONE = "standalone"


class CoordinationError(RuntimeError):
    """Base error for ScreenLock / Control Plane coordination failures."""

    code: str = "coordination_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CoordinationConfigError(CoordinationError):
    """Invalid or incomplete coordination configuration (fail-closed)."""

    code: str = "coordination_config_error"


class ScreenLockBusy(CoordinationError):
    """Device is currently locked/reserved by another workflow or session."""

    code: str = "screen_lock_busy"


class ScreenQuarantineMarkerPresent(ScreenLockBusy):
    """Device has an active ScreenLock quarantine marker; never bypassed."""

    code: str = "screen_quarantine_marker_present"

    def __init__(self, device: str, path: Path | str | None = None, *, detail: str = "") -> None:
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"device={device!r} quarantined: device_quiesce_unverified{suffix}",
            code=self.code,
        )
        self.device = device
        self.path = Path(path) if isinstance(path, (str, Path)) and str(path) else None


class ControlPlaneUnavailable(CoordinationError):
    """Control Plane is configured but unreachable or rejected coordination."""

    code: str = "control_plane_unavailable"


class LockRequiredError(CoordinationError):
    """Strict lock mode (VPHONE_MCP_REQUIRE_LOCK=1) refused uncoordinated write."""

    code: str = "lock_required"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_bool_env(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = str(env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _parse_bounded_float(
    env: Mapping[str, str],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = str(env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise CoordinationConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not (minimum <= value <= maximum):
        raise CoordinationConfigError(
            f"{key} must be between {minimum} and {maximum}, got {value}"
        )
    return value


def validate_device_name(device: str) -> str:
    value = str(device or "").strip()
    if not value or value in {".", ".."} or not _DEVICE_NAME_RE.fullmatch(value):
        raise CoordinationConfigError(
            f"invalid device/rig id {device!r}: must match [A-Za-z0-9._:-]+ and not be '.' or '..'"
        )
    return value


def _normalize_http_base_url(raw: str, *, source: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise CoordinationConfigError(
            f"{source} must be an absolute http(s) URL, got {raw!r}"
        )
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _derive_http_url_from_ws(ws_url: str) -> str:
    parts = urlsplit(ws_url.strip())
    if parts.scheme not in {"ws", "wss"} or not parts.netloc:
        raise CoordinationConfigError(
            f"AUTOPHONE_CONTROL_WS must be an absolute ws(s) URL, got {ws_url!r}"
        )
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, "", "", ""))


@dataclass(frozen=True)
class CoordinationConfig:
    mode: str
    device: str | None = None
    lock_dir: Path | None = None
    control_plane_url: str | None = None
    control_plane_source: str | None = None
    basic_auth: str | None = None
    require_lock: bool = False
    lock_ttl_s: float = DEFAULT_LOCK_TTL_S
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S


def resolve_coordination_config(
    env: Mapping[str, str] | None = None,
) -> CoordinationConfig:
    """Resolve coordination mode and settings from environment variables."""
    source = os.environ if env is None else env
    require_lock = _parse_bool_env(source, "VPHONE_MCP_REQUIRE_LOCK", default=False)
    lock_ttl_s = _parse_bounded_float(
        source,
        "VPHONE_MCP_LOCK_TTL_S",
        DEFAULT_LOCK_TTL_S,
        minimum=MIN_LOCK_TTL_S,
        maximum=MAX_LOCK_TTL_S,
    )
    http_timeout_s = _parse_bounded_float(
        source,
        "VPHONE_MCP_HTTP_TIMEOUT_S",
        DEFAULT_HTTP_TIMEOUT_S,
        minimum=0.2,
        maximum=60.0,
    )

    raw_device = (
        str(source.get("VPHONE_DEVICE") or "").strip()
        or str(source.get("AUTOPHONE_DEVICE") or "").strip()
        or str(source.get("AUTOPHONE_RIG_ID") or "").strip()
        or str(source.get("AP_DEVICE") or "").strip()
    )
    device = validate_device_name(raw_device) if raw_device else None

    # Managed AI agent spawned inside Autophone's StageGuard + AgentDeviceProxy:
    # parent process already holds ScreenLock + StageGuard for the device.
    if str(source.get("AUTOPHONE_MANAGED_DEVICE_AGENT") or "").strip() == "1":
        return CoordinationConfig(
            mode=MODE_MANAGED_PROXY,
            device=device,
            require_lock=require_lock,
            lock_ttl_s=lock_ttl_s,
            http_timeout_s=http_timeout_s,
        )

    # Local lock directory resolution (mirrors autophone/device/lock.py).
    explicit_lock_dir = (
        str(source.get("VPHONE_MCP_LOCK_DIR") or "").strip()
        or str(source.get("AUTOPHONE_LOCK_DIR") or "").strip()
    )
    state_dir = str(source.get("AUTOPHONE_STATE_DIR") or "").strip()
    lock_dir: Path | None = None
    if explicit_lock_dir:
        lock_dir = Path(explicit_lock_dir).expanduser().resolve()
    elif state_dir:
        lock_dir = (Path(state_dir).expanduser().resolve()) / "runs" / ".locks"

    # Control Plane HTTP endpoint resolution.
    cp_url: str | None = None
    cp_source: str | None = None
    for env_key in ("AUTOPHONE_CONTROL_PLANE_URL", "AUTOPHONE_CONTROL_HTTP", "AUTOPHONE_API_URL"):
        val = str(source.get(env_key) or "").strip()
        if val:
            cp_url = _normalize_http_base_url(val, source=env_key)
            cp_source = env_key
            break
    if cp_url is None:
        ws_val = str(source.get("AUTOPHONE_CONTROL_WS") or "").strip()
        if ws_val:
            cp_url = _derive_http_url_from_ws(ws_val)
            cp_source = "AUTOPHONE_CONTROL_WS"
    if cp_url is None and str(source.get("AUTOPHONE_DATABASE_URL") or "").strip():
        # Control Plane DSN is configured on this host: fail closed via default
        # Control Plane HTTP address instead of bypassing to raw UDS.
        cp_url = DEFAULT_CONTROL_PLANE_URL
        cp_source = "AUTOPHONE_DATABASE_URL"

    basic_auth = (
        str(source.get("AUTOPHONE_CONTROL_PLANE_AUTH") or "").strip()
        or str(source.get("AUTOPHONE_API_AUTH") or "").strip()
        or None
    )

    if cp_url is not None:
        if device is None:
            raise CoordinationConfigError(
                f"Control Plane coordination ({cp_source}) requires a device/rig id "
                "(set VPHONE_DEVICE, AUTOPHONE_DEVICE, AUTOPHONE_RIG_ID, or AP_DEVICE)"
            )
        return CoordinationConfig(
            mode=MODE_CONTROL_PLANE,
            device=device,
            lock_dir=lock_dir,
            control_plane_url=cp_url,
            control_plane_source=cp_source,
            basic_auth=basic_auth,
            require_lock=require_lock,
            lock_ttl_s=lock_ttl_s,
            http_timeout_s=http_timeout_s,
        )

    if explicit_lock_dir and device is None:
        raise CoordinationConfigError(
            "Explicit ScreenLock directory requires a device/rig id "
            "(set VPHONE_DEVICE, AUTOPHONE_DEVICE, AUTOPHONE_RIG_ID, or AP_DEVICE)"
        )

    if lock_dir is not None and device is not None:
        return CoordinationConfig(
            mode=MODE_LOCAL_LOCK,
            device=device,
            lock_dir=lock_dir,
            require_lock=require_lock,
            lock_ttl_s=lock_ttl_s,
            http_timeout_s=http_timeout_s,
        )

    return CoordinationConfig(
        mode=MODE_STANDALONE,
        device=device,
        lock_dir=lock_dir,
        require_lock=require_lock,
        lock_ttl_s=lock_ttl_s,
        http_timeout_s=http_timeout_s,
    )


def _format_owner_id(tool: str) -> str:
    safe_tool = re.sub(r"[^A-Za-z0-9._-]+", "_", str(tool or "call").strip())[:40] or "call"
    return f"vphone-mcp:{os.getpid()}:{safe_tool}-{uuid.uuid4().hex[:8]}"


@dataclass
class LocalScreenLease:
    """Active local ScreenLock flock lease held by a vphone_mcp call."""

    lock: "LocalScreenLock"
    device: str
    owner_kind: str
    owner_id: str
    fh: Any
    acquired_at: str
    expires_at: str
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.lock._release_lease(self)


class LocalScreenLock:
    """File-based per-Rig ScreenLock compatible with ``autophone.device.lock.ScreenLock``.

    - No directory creation on ``__init__`` or read-only status calls (matches BL-122).
    - Non-blocking ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``<root>/<device>.lock``.
    - Quarantine check (``<root>/<device>.quarantine.json``) performed *while holding
      the flock* so a quarantine written by a preceding owner is always observed.
    - State file (``<root>/<device>.json``) uses ``owner_kind="run"`` (the valid
      Autophone atomic-operation owner kind, matching ``_op_system_location``) and
      ``owner_id="vphone-mcp:<pid>:<call>"`` so Autophone's ``ScreenLock.status()``,
      ``live_status()``, and ``read_human_holder()`` validate it cleanly.
    """

    OWNER_KIND = "run"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def _lock_path(self, device: str) -> Path:
        return self.root / f"{device}.lock"

    def _state_path(self, device: str) -> Path:
        return self.root / f"{device}.json"

    def _quarantine_path(self, device: str) -> Path:
        return self.root / f"{device}.quarantine.json"

    def status(self, *, device: str) -> dict[str, Any]:
        path = self._state_path(device)
        empty: dict[str, Any] = {
            "device": device,
            "locked": False,
            "run_id": None,
            "acquired_at": None,
            "pid": None,
            "owner_kind": None,
            "owner_id": None,
        }
        if not path.exists():
            return empty
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        return data if isinstance(data, dict) else empty

    def quarantine_status(self, *, device: str) -> dict[str, Any] | None:
        path = self._quarantine_path(device)
        if not path.exists():
            return None
        info: dict[str, Any] = {"device": device, "path": str(path)}
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(marker, dict):
                info["marker"] = marker
        except (OSError, json.JSONDecodeError) as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    def _write_state(self, device: str, payload: Mapping[str, Any]) -> None:
        self._ensure_root()
        path = self._state_path(device)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def acquire_lease(
        self,
        *,
        device: str,
        owner_id: str,
        ttl_s: float = DEFAULT_LOCK_TTL_S,
        tool: str = "",
    ) -> LocalScreenLease:
        validate_device_name(device)
        self._ensure_root()
        lock_path = self._lock_path(device)
        fh = lock_path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            holder = self.status(device=device)
            owner_kind = holder.get("owner_kind") or "workflow"
            holder_id = holder.get("owner_id") or holder.get("run_id") or "unknown"
            pid = holder.get("pid")
            pid_suffix = f" (pid={pid})" if pid else ""
            raise ScreenLockBusy(
                f"device={device!r} is locked by {owner_kind} {holder_id!r}{pid_suffix}"
            ) from None

        try:
            quarantine_path = self._quarantine_path(device)
            if quarantine_path.exists():
                raise ScreenQuarantineMarkerPresent(device, quarantine_path)
            now = _now_utc()
            acquired_at = _iso_utc(now)
            expires_at = _iso_utc(now + timedelta(seconds=ttl_s))
            self._write_state(
                device,
                {
                    "device": device,
                    "locked": True,
                    "run_id": owner_id,
                    "acquired_at": acquired_at,
                    "expires_at": expires_at,
                    "pid": os.getpid(),
                    "owner_kind": self.OWNER_KIND,
                    "owner_id": owner_id,
                    "client": "vphone_mcp",
                    "tool": tool,
                },
            )
        except BaseException:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()
            raise

        return LocalScreenLease(
            lock=self,
            device=device,
            owner_kind=self.OWNER_KIND,
            owner_id=owner_id,
            fh=fh,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )

    def _release_lease(self, lease: LocalScreenLease) -> None:
        try:
            state = self.status(device=lease.device)
            if (
                state.get("owner_kind") == lease.owner_kind
                and state.get("owner_id") == lease.owner_id
            ):
                self._write_state(
                    lease.device,
                    {
                        "device": lease.device,
                        "locked": False,
                        "run_id": None,
                        "acquired_at": None,
                        "pid": None,
                        "owner_kind": None,
                        "owner_id": None,
                    },
                )
        finally:
            try:
                fcntl.flock(lease.fh.fileno(), fcntl.LOCK_UN)
            finally:
                lease.fh.close()


HttpOpener = Callable[[urllib.request.Request, float], Any]


def _default_http_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - user-configured Control Plane URL


def _extract_http_error_detail(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Return (code, message) parsed from a FastAPI/Autophone HTTP error body."""
    raw = ""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, dict):
        code = str(detail.get("code") or "")
        message = str(detail.get("message") or detail or exc.reason or f"HTTP {exc.code}")
        return code, message
    if detail is not None:
        return "", str(detail)
    return "", raw.strip() or str(exc.reason or f"HTTP {exc.code}")


class ControlPlaneHttpClient:
    """Minimal stdlib HTTP client for real Autophone Control Plane & API endpoints.

    Endpoints used (verified against ``autophone/control/`` and
    ``autophone/adapters/http/``):
    - ``GET /health`` -> identifies ``autophone-control-plane`` vs ``autophone-run``
    - ``POST /api/control/reservations`` &
      ``POST /api/control/reservations/{reservation_id}/release`` -> Rig reservations
    - ``POST /api/control/device-sessions/{device}/ops`` (``op="device_status"``) ->
      live Rig ScreenLock & quarantine inspection on ``autophone-control-plane``
    - ``GET /api/platform/targets`` & ``GET /devices/{device}/status`` ->
      mode discovery and live Rig ScreenLock & quarantine inspection on ``autophone-run``
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        basic_auth: str | None = None,
        opener: HttpOpener | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.basic_auth = basic_auth
        self._opener = opener or _default_http_opener

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.basic_auth:
            token = base64.b64encode(self.basic_auth.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _json_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(dict(payload)).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers=self._headers(json_body=payload is not None),
            method=method,
        )
        try:
            with self._opener(req, self.timeout_s) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport error fails closed
            raise ControlPlaneUnavailable(
                f"Control Plane unreachable at {self.base_url}{path}: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControlPlaneUnavailable(
                f"Control Plane returned invalid JSON from {path}: {exc}"
            ) from exc
        if not isinstance(doc, dict):
            raise ControlPlaneUnavailable(f"Control Plane returned non-object JSON from {path}")
        return status, doc

    def check_health(self) -> dict[str, Any]:
        """Verify the configured Control Plane / API endpoint is reachable and healthy."""
        try:
            status, doc = self._json_request("GET", "/health")
        except urllib.error.HTTPError as exc:
            _code, msg = _extract_http_error_detail(exc)
            raise ControlPlaneUnavailable(
                f"Control Plane health check failed ({exc.code}): {msg}"
            ) from exc
        if status != 200 or doc.get("ok") is not True:
            raise ControlPlaneUnavailable(
                f"Control Plane health check unhealthy at {self.base_url}/health: {doc!r}"
            )
        return doc

    def platform_mode(self) -> str | None:
        """Query ``GET /api/platform/targets`` on ``autophone-run`` for local vs control mode."""
        try:
            _status, doc = self._json_request("GET", "/api/platform/targets")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            _code, msg = _extract_http_error_detail(exc)
            raise ControlPlaneUnavailable(
                f"Control Plane platform targets query failed ({exc.code}): {msg}"
            ) from exc
        mode = doc.get("mode")
        return str(mode) if isinstance(mode, str) else None

    def create_reservation(
        self,
        *,
        device: str,
        owner_id: str,
        tool: str,
        ttl_s: float,
    ) -> str:
        expires_at = _iso_utc(_now_utc() + timedelta(seconds=ttl_s))
        payload = {
            "rig_id": device,
            "owner": {
                "kind": "vphone_mcp",
                "owner_id": owner_id,
                "tool": tool,
                "pid": os.getpid(),
            },
            "expires_at": expires_at,
        }
        try:
            _status, doc = self._json_request("POST", "/api/control/reservations", payload)
        except urllib.error.HTTPError as exc:
            code, msg = _extract_http_error_detail(exc)
            lowered = f"{code} {msg}".lower()
            if exc.code in (409, 422) and (
                "active reservation" in lowered or "busy" in lowered or "reserved" in lowered
            ):
                raise ScreenLockBusy(
                    f"device={device!r} already has an active Control Plane reservation: {msg}"
                ) from exc
            raise ControlPlaneUnavailable(
                f"Control Plane refused reservation for {device!r} ({exc.code}): {msg}"
            ) from exc
        reservation_id = str(doc.get("reservation_id") or "").strip()
        if not reservation_id:
            raise ControlPlaneUnavailable(
                f"Control Plane reservation response missing reservation_id: {doc!r}"
            )
        return reservation_id

    def release_reservation(self, reservation_id: str, *, actor: str) -> None:
        encoded = quote(reservation_id, safe="")
        try:
            self._json_request(
                "POST",
                f"/api/control/reservations/{encoded}/release",
                {"actor": actor},
            )
        except urllib.error.HTTPError as exc:
            _code, msg = _extract_http_error_detail(exc)
            raise ControlPlaneUnavailable(
                f"Failed to release Control Plane reservation {reservation_id!r} ({exc.code}): {msg}"
            ) from exc

    def fetch_device_status(self, *, device: str, service: str) -> dict[str, Any]:
        """Inspect live Rig ScreenLock and quarantine status via real Autophone endpoints."""
        encoded = quote(device, safe="")
        if service == "autophone-run":
            try:
                _status, doc = self._json_request("GET", f"/devices/{encoded}/status")
            except urllib.error.HTTPError as exc:
                code, msg = _extract_http_error_detail(exc)
                if exc.code == 409 and ("busy" in code.lower() or "busy" in msg.lower()):
                    raise ScreenLockBusy(f"device={device!r} busy: {msg}") from exc
                raise ControlPlaneUnavailable(
                    f"Device status unavailable for {device!r} ({exc.code}): {msg}"
                ) from exc
            return doc

        # Standalone Control Plane: POST /api/control/device-sessions/{device}/ops
        try:
            _status, doc = self._json_request(
                "POST",
                f"/api/control/device-sessions/{encoded}/ops",
                {"op": "device_status", "payload": {}},
            )
        except urllib.error.HTTPError as exc:
            code, msg = _extract_http_error_detail(exc)
            if exc.code == 409 and ("busy" in code.lower() or "busy" in msg.lower()):
                raise ScreenLockBusy(f"device={device!r} busy: {msg}") from exc
            raise ControlPlaneUnavailable(
                f"Control Plane device_status failed for {device!r} ({exc.code}): {msg}"
            ) from exc
        if doc.get("ok") is not True or not isinstance(doc.get("result"), dict):
            raise ControlPlaneUnavailable(
                f"Control Plane device_status returned unexpected body for {device!r}: {doc!r}"
            )
        return dict(doc["result"])


def _assert_device_status_clear(device: str, status: Mapping[str, Any]) -> None:
    quarantine = status.get("screen_quarantine")
    if quarantine:
        path = quarantine.get("path") if isinstance(quarantine, Mapping) else None
        raise ScreenQuarantineMarkerPresent(device, path)
    lock_info = status.get("lock")
    if isinstance(lock_info, Mapping) and lock_info.get("locked") is True:
        owner_kind = lock_info.get("owner_kind") or "workflow"
        owner_id = lock_info.get("owner_id") or lock_info.get("run_id") or "unknown"
        pid = lock_info.get("pid")
        pid_suffix = f" (pid={pid})" if pid else ""
        raise ScreenLockBusy(
            f"device={device!r} is locked by {owner_kind} {owner_id!r}{pid_suffix}"
        )


@dataclass
class ActiveLease:
    """Context-bound active coordination lease for the current mutating call."""

    coordinator_id: int
    mode: str
    device: str | None
    owner_id: str
    deadline_monotonic: float
    reservation_id: str | None = None
    local_lease: LocalScreenLease | None = None


_ACTIVE_LEASE: ContextVar[ActiveLease | None] = ContextVar("vphone_mcp_active_lease", default=None)
_STANDALONE_WARNING_EMITTED = False


class DeviceCoordinator:
    """Enforces ScreenLock and Control Plane coordination around device operations."""

    def __init__(
        self,
        config: CoordinationConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
        http_opener: HttpOpener | None = None,
    ) -> None:
        self.config = config if config is not None else resolve_coordination_config(env)
        self._local_lock = (
            LocalScreenLock(self.config.lock_dir)
            if self.config.lock_dir is not None
            else None
        )
        self._cp_client = (
            ControlPlaneHttpClient(
                self.config.control_plane_url,
                timeout_s=self.config.http_timeout_s,
                basic_auth=self.config.basic_auth,
                opener=http_opener,
            )
            if self.config.control_plane_url is not None
            else None
        )

    @property
    def mode(self) -> str:
        return self.config.mode

    def remaining_timeout_s(self, requested: float) -> float:
        """Bound a UDS timeout to the active lease's TTL budget when a lease is held."""
        lease = _ACTIVE_LEASE.get()
        if lease is None or lease.coordinator_id != id(self):
            return requested
        remaining = lease.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise CoordinationError(
                f"ScreenLock lease TTL ({self.config.lock_ttl_s}s) exhausted before device call",
                code="lease_ttl_expired",
            )
        return min(requested, remaining)

    def check_read_access(self, tool: str = "read") -> None:
        """Enforce fail-closed Control Plane reachability on read-only operations.

        Read-only operations never acquire ScreenLock or a Rig reservation so they
        do not contend with managed regression runs. However, when a Control Plane
        is configured and unreachable, we still fail closed instead of silently
        falling back to raw socket access.
        """
        if self.config.mode == MODE_CONTROL_PLANE:
            assert self._cp_client is not None
            self._cp_client.check_health()

    def status(self) -> dict[str, Any]:
        """Return a read-only summary of coordination mode, lock, and quarantine state."""
        summary: dict[str, Any] = {
            "mode": self.config.mode,
            "device": self.config.device,
            "require_lock": self.config.require_lock,
            "lock_ttl_s": self.config.lock_ttl_s,
            "lock_dir": str(self.config.lock_dir) if self.config.lock_dir is not None else None,
            "control_plane_url": self.config.control_plane_url,
            "control_plane_source": self.config.control_plane_source,
            "coordinated": self.config.mode in {
                MODE_CONTROL_PLANE,
                MODE_LOCAL_LOCK,
                MODE_MANAGED_PROXY,
            },
        }
        if self._local_lock is not None and self.config.device:
            summary["local_lock"] = self._local_lock.status(device=self.config.device)
            summary["local_quarantine"] = self._local_lock.quarantine_status(
                device=self.config.device
            )
        return summary

    @contextmanager
    def mutating_call(self, tool: str) -> Iterator[ActiveLease | None]:
        """Acquire ScreenLock / Control Plane coordination for a mutating tool call.

        Re-entrant within the same execution context so composite tools (e.g.,
        ``open_app`` doing ``key("home")`` + ``tap(...)`` + follow-up ``screenshot()``)
        hold a single exclusive lease across all sub-operations and release cleanly
        in ``finally``.
        """
        existing = _ACTIVE_LEASE.get()
        if existing is not None and existing.coordinator_id == id(self):
            yield existing
            return

        mode = self.config.mode
        if mode == MODE_MANAGED_PROXY:
            yield None
            return

        if mode == MODE_STANDALONE:
            if self.config.require_lock:
                raise LockRequiredError(
                    f"Refusing mutating tool {tool!r}: VPHONE_MCP_REQUIRE_LOCK=1 is set "
                    "but neither Control Plane coordination (AUTOPHONE_CONTROL_PLANE_URL / "
                    "AUTOPHONE_API_URL + VPHONE_DEVICE) nor local ScreenLock "
                    "(AUTOPHONE_STATE_DIR / VPHONE_MCP_LOCK_DIR + VPHONE_DEVICE) is configured."
                )
            self._warn_standalone_once()
            yield None
            return

        owner_id = _format_owner_id(tool)
        deadline = time.monotonic() + self.config.lock_ttl_s
        device = self.config.device
        assert device is not None

        if mode == MODE_LOCAL_LOCK:
            assert self._local_lock is not None
            local_lease = self._local_lock.acquire_lease(
                device=device,
                owner_id=owner_id,
                ttl_s=self.config.lock_ttl_s,
                tool=tool,
            )
            active = ActiveLease(
                coordinator_id=id(self),
                mode=mode,
                device=device,
                owner_id=owner_id,
                deadline_monotonic=deadline,
                local_lease=local_lease,
            )
            token = _ACTIVE_LEASE.set(active)
            try:
                yield active
            finally:
                _ACTIVE_LEASE.reset(token)
                local_lease.release()
            return

        # MODE_CONTROL_PLANE
        assert self._cp_client is not None
        health = self._cp_client.check_health()
        service = str(health.get("service") or "autophone-control-plane")

        use_reservation = service != "autophone-run"
        if service == "autophone-run":
            platform_mode = self._cp_client.platform_mode()
            use_reservation = platform_mode == "control"

        reservation_id: str | None = None
        local_lease: LocalScreenLease | None = None
        try:
            if use_reservation:
                reservation_id = self._cp_client.create_reservation(
                    device=device,
                    owner_id=owner_id,
                    tool=tool,
                    ttl_s=self.config.lock_ttl_s,
                )
            # Check live Rig ScreenLock & quarantine state BEFORE touching UDS.
            device_status = self._cp_client.fetch_device_status(device=device, service=service)
            _assert_device_status_clear(device, device_status)

            # Layer local ScreenLock if a lock directory is also configured on this host.
            if self._local_lock is not None:
                local_lease = self._local_lock.acquire_lease(
                    device=device,
                    owner_id=owner_id,
                    ttl_s=self.config.lock_ttl_s,
                    tool=tool,
                )
            elif not use_reservation and self.config.require_lock:
                raise LockRequiredError(
                    f"Refusing mutating tool {tool!r}: {self.config.control_plane_url} is in "
                    "local mode without Control Plane reservations and no local ScreenLock "
                    "directory (AUTOPHONE_STATE_DIR / VPHONE_MCP_LOCK_DIR) is configured."
                )

            active = ActiveLease(
                coordinator_id=id(self),
                mode=mode,
                device=device,
                owner_id=owner_id,
                deadline_monotonic=deadline,
                reservation_id=reservation_id,
                local_lease=local_lease,
            )
            token = _ACTIVE_LEASE.set(active)
            try:
                yield active
            finally:
                _ACTIVE_LEASE.reset(token)
        finally:
            if local_lease is not None:
                local_lease.release()
            if reservation_id is not None:
                try:
                    self._cp_client.release_reservation(reservation_id, actor=owner_id)
                except Exception as exc:  # noqa: BLE001 - do not mask tool exceptions
                    logger.warning(
                        "Failed to release Control Plane reservation %s (TTL=%ss): %s",
                        reservation_id,
                        self.config.lock_ttl_s,
                        exc,
                    )

    def _warn_standalone_once(self) -> None:
        global _STANDALONE_WARNING_EMITTED
        if _STANDALONE_WARNING_EMITTED:
            return
        _STANDALONE_WARNING_EMITTED = True
        extra = ""
        if self.config.lock_dir is not None and self.config.device is None:
            extra = " (lock directory detected, but VPHONE_DEVICE/AUTOPHONE_DEVICE/AP_DEVICE is unset)"
        logger.warning(
            "vphone_mcp running in uncoordinated standalone VPHONE_SOCK mode%s; "
            "configure AUTOPHONE_STATE_DIR + VPHONE_DEVICE or AUTOPHONE_CONTROL_PLANE_URL + "
            "VPHONE_DEVICE for ScreenLock serialization, or set VPHONE_MCP_REQUIRE_LOCK=1 "
            "to refuse uncoordinated writes.",
            extra,
        )


__all__ = [
    "ActiveLease",
    "ControlPlaneHttpClient",
    "ControlPlaneUnavailable",
    "CoordinationConfig",
    "CoordinationConfigError",
    "CoordinationError",
    "DEFAULT_CONTROL_PLANE_URL",
    "DEFAULT_HTTP_TIMEOUT_S",
    "DEFAULT_LOCK_TTL_S",
    "DeviceCoordinator",
    "LocalScreenLease",
    "LocalScreenLock",
    "LockRequiredError",
    "MODE_CONTROL_PLANE",
    "MODE_LOCAL_LOCK",
    "MODE_MANAGED_PROXY",
    "MODE_STANDALONE",
    "ScreenLockBusy",
    "ScreenQuarantineMarkerPresent",
    "resolve_coordination_config",
    "validate_device_name",
]
