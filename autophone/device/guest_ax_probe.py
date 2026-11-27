"""guest_ax_probe.py — deployment, execution, foreground gating, and schema
normalization for the out-of-process iOS ``guest_axruntime`` probe.

Design boundaries:
- Never modifies ``vphoned`` or ``tools/guest/``; deploys the compiled,
  ad-hoc signed ``guest_ax_probe`` binary into ``/var/jb/tmp/`` keyed by
  SHA-256 prefix (mirroring ``autophone/device/lock_state_helper.py``).
- Never enables VoiceOver and fails closed if ``ax_state.voiceover`` is True.
- Enforces the mandatory ``--pid`` foreground gate: background nodes are
  stripped to ``[]`` and ``foreground_active=False`` is recorded whenever
  the screen is locked, Control Center is visible, or ``primary_app`` is not
  the target bundle/PID.
- Normalizes ``autophone.ax_probe/1`` payloads into canonical ``autophone.ax/1``
  with ``snapshot_kind="ui_structure"`` (without emitting in-process gray-box
  fields such as ``dumper_version`` or ``variant_observation``).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shlex
import tempfile
import threading
import time
from typing import Any, Callable

from autophone.logging import get_logger

logger = get_logger(__name__)

PROBE_SCHEMA = "autophone.ax_probe/1"
PROBE_MIN_VERSION = 1
BUILD_SCHEMA = "autophone.guest-helper-build/v1"
GUEST_DIR = "/var/jb/tmp"
GUEST_PREFIX = "autophone-guest-ax-probe-"
PROBE_ENV = "AUTOPHONE_GUEST_AX_PROBE"

RUN_TIMEOUT_MS = 10_000
DEPLOY_SHELL_TIMEOUT_MS = 10_000
DEPLOY_SOCKET_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_DELAY_MS = 150

_JB_PATH = "export PATH=/var/jb/usr/bin:/var/jb/bin:$PATH"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

RequestFn = Callable[..., Any]


def _unavailable(message: str) -> RuntimeError:
    from autophone.device.ax_session import AxUnavailable

    return AxUnavailable(message)


def _code_root() -> Path:
    from autophone.paths import CODE_ROOT

    return Path(CODE_ROOT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class LocalGuestAxProbe:
    """Verified host-side ``guest_ax_probe`` binary."""

    binary: Path
    binary_sha256: str
    source_sha256: str | None = None

    @property
    def guest_path(self) -> str:
        return f"{GUEST_DIR}/{GUEST_PREFIX}{self.binary_sha256[:16]}"

    def describe(self) -> dict[str, Any]:
        return {
            "binary": str(self.binary),
            "binary_sha256": self.binary_sha256,
            "source_sha256": self.source_sha256,
            "guest_path": self.guest_path,
        }


def resolve_local_probe(
    probe_path: str | Path | None = None,
    code_root: Path | None = None,
) -> LocalGuestAxProbe:
    """Resolve and verify the compiled ``guest_ax_probe`` binary on the host."""
    env_override = os.environ.get(PROBE_ENV, "").strip()
    using_default = probe_path is None and not env_override
    root = Path(code_root) if code_root is not None else _code_root()
    if probe_path is not None:
        binary = Path(probe_path).expanduser().resolve()
    elif env_override:
        binary = Path(env_override).expanduser().resolve()
    else:
        binary = (root / "support" / "guest_ax_probe").resolve()

    if not binary.is_file():
        raise _unavailable(
            f"missing guest_ax_probe binary at {binary}; "
            f"run ./scripts/build_guest_ax_probe.sh or set {PROBE_ENV}"
        )

    binary_sha = _sha256(binary)
    record_path = Path(f"{binary}.build.json")
    source_sha: str | None = None
    if record_path.is_file():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _unavailable(f"invalid guest_ax_probe build record {record_path}: {exc}") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema") != BUILD_SCHEMA
            or record.get("protocol") != PROBE_SCHEMA
            or not _SHA256.match(str(record.get("binary_sha256") or ""))
        ):
            raise _unavailable(f"invalid guest_ax_probe build record {record_path}")
        if record["binary_sha256"] != binary_sha:
            raise _unavailable(
                f"stale guest_ax_probe binary: {binary} sha256 {binary_sha[:12]} "
                f"differs from build record {record['binary_sha256'][:12]}; "
                "rebuild with ./scripts/build_guest_ax_probe.sh"
            )
        raw_src_sha = record.get("source_sha256")
        source_sha = str(raw_src_sha) if isinstance(raw_src_sha, str) and _SHA256.match(raw_src_sha) else None
        if using_default:
            src_file = root / "support" / "guest-ax-probe" / "ax_runtime_probe.m"
            ent_file = root / "support" / "guest-ax-probe" / "entitlements.plist"
            if src_file.is_file():
                current_src_sha = _sha256(src_file)
                if source_sha != current_src_sha:
                    raise _unavailable(
                        f"stale guest_ax_probe binary: built from source {str(source_sha)[:12]}, "
                        f"current ax_runtime_probe.m is {current_src_sha[:12]}; "
                        "rebuild with ./scripts/build_guest_ax_probe.sh"
                    )
            if ent_file.is_file() and record.get("entitlements_sha256") is not None:
                current_ent_sha = _sha256(ent_file)
                if record.get("entitlements_sha256") != current_ent_sha:
                    raise _unavailable(
                        "stale guest_ax_probe binary: entitlements.plist changed; "
                        "rebuild with ./scripts/build_guest_ax_probe.sh"
                    )

    return LocalGuestAxProbe(binary=binary, binary_sha256=binary_sha, source_sha256=source_sha)


_VERIFIED: dict[tuple[str, str], str] = {}
_VERIFIED_LOCK = threading.Lock()


def forget_probe_deployments() -> None:
    """Clear the process-local deployment verification cache (for tests/redeploy)."""
    with _VERIFIED_LOCK:
        _VERIFIED.clear()


def _response(result: Any) -> dict[str, Any]:
    response = result[0] if isinstance(result, tuple) and result else result
    if not isinstance(response, dict):
        raise _unavailable(f"unexpected UDS response {type(response).__name__}")
    return response


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise _unavailable(f"ambiguous AX probe JSON key {key!r}")
        values[key] = value
    return values


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class GuestAxProbeRunner:
    """Deploys and executes ``guest_ax_probe`` on a single guest UDS socket."""

    def __init__(
        self,
        *,
        sock_path: str,
        request_fn: RequestFn | None = None,
        local: LocalGuestAxProbe | None = None,
        probe_path: str | Path | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if request_fn is None:
            from autophone.device.uds import request as request_fn

        self.sock_path = str(Path(sock_path).resolve())
        self._request = request_fn
        self._local = local
        self._probe_path = probe_path
        self.timeout_s = max(0.5, float(timeout_s))
        self._version_cache: dict[tuple[int, str], tuple[str, str]] = {}
        self._capabilities_fetched = False
        self._capabilities_screen: dict[str, float] | None = None

    @property
    def local(self) -> LocalGuestAxProbe:
        if self._local is None:
            self._local = resolve_local_probe(self._probe_path)
        return self._local

    def _shell(self, command: str, *, timeout_ms: int, socket_timeout_s: float) -> dict[str, Any]:
        try:
            result = self._request(
                {"t": "shell", "cmd": f"{_JB_PATH}; {command}", "screen": False, "timeout_ms": timeout_ms},
                sock_path=self.sock_path,
                timeout=socket_timeout_s,
                parse_image=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(f"guest_axruntime shell failed: {type(exc).__name__}: {exc}") from exc
        response = _response(result)
        if response.get("ok") is not True:
            raise _unavailable(f"guest_axruntime shell rejected: {response.get('error')!r}")
        if response.get("timed_out"):
            raise _unavailable(f"guest_axruntime probe timed out after {timeout_ms}ms")
        if response.get("truncated"):
            raise _unavailable("guest_axruntime shell output truncated")
        return response

    def _guest_sha256(self, path: str) -> str | None:
        quoted = shlex.quote(path)
        cmd = (
            f"if [ -f {quoted} ]; then "
            f"if command -v sha256sum >/dev/null 2>&1; then sha256sum -- {quoted}; "
            f"else shasum -a 256 {quoted}; fi; "
            f"else echo ABSENT; fi"
        )
        response = self._shell(
            cmd,
            timeout_ms=DEPLOY_SHELL_TIMEOUT_MS,
            socket_timeout_s=DEPLOY_SOCKET_TIMEOUT_S,
        )
        out = str(response.get("stdout") or "").strip()
        if int(response.get("code", -1)) != 0:
            raise _unavailable(f"guest_ax_probe sha256 check failed (exit {response.get('code')}): {out[:200]!r}")
        if out == "ABSENT":
            return None
        digest = out.split()[0] if out else ""
        if not _SHA256.match(digest):
            raise _unavailable(f"unexpected guest_ax_probe sha256 output {out[:200]!r}")
        return digest

    def _push(self, path: str) -> None:
        try:
            result = self._request(
                {"t": "file_put", "path": path, "load": str(self.local.binary), "perm": "755", "screen": False},
                sock_path=self.sock_path,
                timeout=DEPLOY_SOCKET_TIMEOUT_S,
                parse_image=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(f"guest_ax_probe file_put failed: {type(exc).__name__}: {exc}") from exc
        response = _response(result)
        if response.get("ok") is not True:
            raise _unavailable(f"guest_ax_probe file_put rejected: {response.get('error')!r}")

    def ensure_deployed(self) -> dict[str, Any]:
        """Ensure the content-addressed probe binary exists on guest with matching SHA-256."""
        local = self.local
        key = (self.sock_path, local.binary_sha256)
        with _VERIFIED_LOCK:
            if key in _VERIFIED:
                return {"action": "cached", **local.describe()}
        path = local.guest_path
        found = self._guest_sha256(path)
        action = "verified"
        if found != local.binary_sha256:
            if found is not None:
                logger.warning(
                    "guest_ax_probe hash mismatch on guest, re-pushing: sock=%s path=%s guest_sha256=%s",
                    self.sock_path,
                    path,
                    found,
                )
            self._push(path)
            found = self._guest_sha256(path)
            if found != local.binary_sha256:
                raise _unavailable(
                    f"guest_ax_probe sha256 {found} after push, expected {local.binary_sha256}"
                )
            action = "pushed"
            logger.info("guest_ax_probe deployed: sock=%s path=%s sha256=%s", self.sock_path, path, local.binary_sha256)
        with _VERIFIED_LOCK:
            _VERIFIED[key] = path
        return {"action": action, **local.describe()}

    def _forget(self) -> None:
        if self._local is None:
            return
        with _VERIFIED_LOCK:
            _VERIFIED.pop((self.sock_path, self._local.binary_sha256), None)

    def _probe_guest_path(self) -> str:
        if self._local is not None:
            return self._local.guest_path
        return self.local.guest_path

    def run_probe(
        self,
        *,
        pid: int | None,
        request_id: str,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_ms: int = DEFAULT_RETRY_DELAY_MS,
        hit_test_nodes: bool = False,
        system_wide: bool = False,
        strict_unique_keys: bool = False,
    ) -> dict[str, Any]:
        """Execute ``guest_ax_probe`` once on the guest and validate the raw envelope."""
        guest_path = self._probe_guest_path()
        args = [shlex.quote(guest_path)]
        if pid is not None and pid > 0:
            args.extend(["--pid", str(int(pid))])
        if system_wide:
            args.append("--system-wide")
        if hit_test_nodes:
            args.append("--hit-test-nodes")
        args.extend([
            "--request-id",
            shlex.quote(request_id),
            "--max-retries",
            str(max(0, int(max_retries))),
            "--retry-delay-ms",
            str(max(10, min(2000, int(retry_delay_ms)))),
        ])
        command = " ".join(args)
        timeout_ms = max(1_000, min(RUN_TIMEOUT_MS, int(self.timeout_s * 1000)))
        socket_timeout = max(self.timeout_s, timeout_ms / 1000.0) + 5.0

        response = self._shell(command, timeout_ms=timeout_ms, socket_timeout_s=socket_timeout)
        if int(response.get("code", -1)) in (126, 127):
            logger.warning("guest_ax_probe not executable on guest (exit %s), redeploying", response.get("code"))
            self._forget()
            self.ensure_deployed()
            response = self._shell(command, timeout_ms=timeout_ms, socket_timeout_s=socket_timeout)

        code = int(response.get("code", -1))
        stdout = str(response.get("stdout") or "").strip()
        stderr = str(response.get("stderr") or "").strip()
        if code != 0:
            raise _unavailable(
                f"guest_ax_probe exited {code}: {stderr[-200:] or stdout[-200:]!r}"
            )
        try:
            raw = (
                json.loads(stdout, object_pairs_hook=_unique_pairs)
                if strict_unique_keys
                else json.loads(stdout)
            )
        except Exception as exc:
            if type(exc).__name__ == "AxUnavailable":
                raise
            raise _unavailable(f"invalid guest_ax_probe JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise _unavailable("guest_ax_probe output must be a JSON object")
        if raw.get("schema") != PROBE_SCHEMA:
            raise _unavailable(f"unexpected guest_ax_probe schema {raw.get('schema')!r}, want {PROBE_SCHEMA!r}")

        probe_version = raw.get("probe_version")
        if probe_version is not None:
            if isinstance(probe_version, bool) or not isinstance(probe_version, int) or probe_version < PROBE_MIN_VERSION:
                raise _unavailable(
                    f"stale guest_ax_probe version={probe_version!r} < {PROBE_MIN_VERSION}"
                )

        echoed_req = raw.get("request_id")
        if isinstance(echoed_req, str) and echoed_req and echoed_req != request_id:
            raise _unavailable(
                f"guest_ax_probe request_id mismatch: got={echoed_req!r}, want={request_id!r}"
            )

        if _voiceover_enabled(raw.get("ax_state")):
            raise _unavailable("guest_axruntime: VoiceOver unexpectedly enabled; refusing AX snapshot")

        return raw

    def resolve_running_app(self, bundle_id: str) -> tuple[int, dict[str, Any]]:
        """Resolve the single running PID for ``bundle_id`` via UDS ``app_list``."""
        try:
            result = self._request(
                {"t": "app_list", "filter": "running", "screen": False},
                sock_path=self.sock_path,
                timeout=self.timeout_s,
                parse_image=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(f"guest_axruntime app_list failed: {type(exc).__name__}: {exc}") from exc
        response = _response(result)
        if response.get("ok") is False:
            raise _unavailable(f"guest_axruntime app_list rejected: {response.get('error')!r}")
        rows = [
            dict(row)
            for row in (response.get("apps") or [])
            if isinstance(row, Mapping)
            and str(row.get("bundle_id") or "") == bundle_id
            and (_positive_int(row.get("pid")) is not None)
        ]
        if not rows:
            raise _unavailable(f"guest_axruntime: {bundle_id} is not running")
        if len(rows) > 1:
            raise _unavailable(f"guest_axruntime: ambiguous running PIDs for {bundle_id}")
        return int(rows[0]["pid"]), rows[0]

    def resolve_bundle_versions(
        self,
        bundle_id: str,
        pid: int,
        probe_raw: Mapping[str, Any],
        app_row: Mapping[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Resolve ``(short_version, bundle_version)`` from Guest probe or UDS Info.plist."""
        short_ver = str(probe_raw.get("short_version") or "").strip()
        bundle_ver = str(probe_raw.get("bundle_version") or "").strip()

        cached = self._version_cache.get((pid, bundle_id))
        if cached is not None:
            if not short_ver:
                short_ver = cached[0]
            if not bundle_ver:
                bundle_ver = cached[1]

        if isinstance(app_row, Mapping):
            if not short_ver:
                short_ver = str(app_row.get("short_version") or app_row.get("version") or "").strip()
            if not bundle_ver:
                bundle_ver = str(app_row.get("build_version") or "").strip()

        if (not short_ver or not bundle_ver) and isinstance(app_row, Mapping):
            install_path = str(app_row.get("path") or "").strip()
            if install_path:
                try:
                    with tempfile.TemporaryDirectory(prefix="autophone-ax-plist-") as tmp:
                        local_plist = Path(tmp) / "Info.plist"
                        resp_raw = self._request(
                            {
                                "t": "file_get",
                                "path": f"{install_path.rstrip('/')}/Info.plist",
                                "save": str(local_plist),
                                "screen": False,
                            },
                            sock_path=self.sock_path,
                            timeout=self.timeout_s,
                            parse_image=False,
                        )
                        resp = _response(resp_raw)
                        if resp.get("ok") and local_plist.is_file():
                            parsed = plistlib.loads(local_plist.read_bytes())
                            if isinstance(parsed, Mapping):
                                if not short_ver:
                                    short_ver = str(parsed.get("CFBundleShortVersionString") or "").strip()
                                if not bundle_ver:
                                    bundle_ver = str(parsed.get("CFBundleVersion") or "").strip()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("guest_axruntime Info.plist fallback failed for %s: %s", bundle_id, exc)

        if short_ver and bundle_ver and pid > 0:
            self._version_cache[(pid, bundle_id)] = (short_ver, bundle_ver)
        return short_ver, bundle_ver

    def fetch_capabilities_screen(self, *, force: bool = False) -> dict[str, float] | None:
        """Fetch and cache ``capabilities.screen`` via UDS without hardcoding dimensions."""
        if self._capabilities_fetched and not force:
            return self._capabilities_screen
        from autophone.device.uds import get_capabilities

        self._capabilities_fetched = True
        try:
            caps = get_capabilities(
                sock_path=self.sock_path,
                timeout=min(5.0, self.timeout_s),
                request_fn=self._request,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("guest_axruntime capabilities lookup unavailable: %s", exc)
            self._capabilities_screen = None
            return None
        if caps.get("available") is not True:
            self._capabilities_screen = None
            return None
        screen = caps.get("screen")
        if not isinstance(screen, Mapping):
            self._capabilities_screen = None
            return None
        w, h, scale = screen.get("width"), screen.get("height"), screen.get("scale")
        if (
            not _is_finite_number(w)
            or not _is_finite_number(h)
            or not _is_finite_number(scale)
            or float(w) <= 0
            or float(h) <= 0
            or float(scale) <= 0
        ):
            self._capabilities_screen = None
            return None
        self._capabilities_screen = {
            "width": float(w),
            "height": float(h),
            "scale": float(scale),
        }
        return self._capabilities_screen


def _voiceover_enabled(ax_state: Any) -> bool:
    if not isinstance(ax_state, Mapping):
        return False
    val = ax_state.get("voiceover")
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    return False


def _evaluate_foreground_gate(
    app: str,
    pid: int | None,
    system: Mapping[str, Any],
) -> tuple[bool, str]:
    if system.get("is_screen_locked") is True:
        return False, "screen_locked"
    if app != "com.apple.springboard":
        if system.get("is_control_center_visible") is True:
            return False, "control_center_visible"
        primary = system.get("primary_app")
        if not isinstance(primary, Mapping) or not str(primary.get("bundle_id") or "").strip():
            return False, "primary_app_missing"
        prim_bundle = str(primary.get("bundle_id") or "").strip()
        if prim_bundle != app:
            return False, f"foreground_app_mismatch:{prim_bundle}"
        prim_pid = _positive_int(primary.get("pid"))
        if pid is not None and pid > 0 and prim_pid is not None and prim_pid != pid:
            return False, f"foreground_pid_mismatch:{prim_pid}!={pid}"
        occluded_scenes = system.get("focused_occluded_app_scene_identifiers")
        if isinstance(occluded_scenes, list):
            for scene in occluded_scenes:
                if isinstance(scene, str) and app in scene:
                    return False, "scene_occluded"
    return True, ""


def _resolve_screen_geometry(
    probe_screen: Mapping[str, Any],
    capabilities_screen: Mapping[str, Any] | None,
    *,
    foreground_active: bool,
) -> tuple[float, float, float, float]:
    """Return ``(w_pt, h_pt, scale, coord_divisor)`` or fail closed."""
    px = probe_screen.get("x", 0.0)
    py = probe_screen.get("y", 0.0)
    if not _is_finite_number(px) or not _is_finite_number(py):
        raise _unavailable("guest_axruntime: non-finite screen origin")
    if abs(float(px)) > 1.0 or abs(float(py)) > 1.0:
        raise _unavailable(f"guest_axruntime: unexpected screen origin ({px}, {py})")

    pw_raw = probe_screen.get("w", 0.0)
    ph_raw = probe_screen.get("h", 0.0)
    pscale_raw = probe_screen.get("scale", 0.0)
    if not _is_finite_number(pw_raw) or not _is_finite_number(ph_raw) or not _is_finite_number(pscale_raw):
        raise _unavailable("guest_axruntime: non-finite screen geometry")
    pw, ph, pscale = float(pw_raw), float(ph_raw), float(pscale_raw)

    if capabilities_screen is not None:
        cap_w = float(capabilities_screen["width"])
        cap_h = float(capabilities_screen["height"])
        cap_scale = float(capabilities_screen["scale"])
        cap_pt_w = cap_w / cap_scale
        cap_pt_h = cap_h / cap_scale

        if not foreground_active:
            # No nodes are emitted while occluded/locked, and iOS 27.0.1 (24A446)
            # reports a scale-divided SpringBoard frame (w/scale x h/scale) while
            # the display is locked/sleeping; preserve foreground_occlusion_reason.
            return cap_pt_w, cap_pt_h, cap_scale, 1.0

        if pw > 0 and ph > 0:
            matches_pt = (
                (abs(pw - cap_pt_w) <= 2.0 and abs(ph - cap_pt_h) <= 2.0)
                or (abs(pw - cap_pt_h) <= 2.0 and abs(ph - cap_pt_w) <= 2.0)
            )
            matches_px = (
                (abs(pw - cap_w) <= 2.0 and abs(ph - cap_h) <= 2.0)
                or (abs(pw - cap_h) <= 2.0 and abs(ph - cap_w) <= 2.0)
            )
            if matches_pt:
                if pscale > 0 and abs(pscale - cap_scale) > 0.05:
                    raise _unavailable(
                        f"guest_axruntime: screen scale mismatch probe={pscale} capabilities={cap_scale}"
                    )
                return pw, ph, cap_scale, 1.0
            if matches_px:
                if pscale > 0 and abs(pscale - cap_scale) > 0.05:
                    raise _unavailable(
                        f"guest_axruntime: screen scale mismatch probe={pscale} capabilities={cap_scale}"
                    )
                return pw / cap_scale, ph / cap_scale, cap_scale, cap_scale
            raise _unavailable(
                f"guest_axruntime: screen geometry mismatch probe=({pw}x{ph}) "
                f"capabilities=({cap_pt_w}x{cap_pt_h}@{cap_scale})"
            )
        raise _unavailable(f"guest_axruntime: invalid screen dimensions w={pw} h={ph}")

    if pw <= 0 or ph <= 0 or pscale <= 0:
        raise _unavailable(
            f"guest_axruntime: unable to determine screen scale/dimensions (w={pw}, h={ph}, scale={pscale})"
        )
    return pw, ph, pscale, 1.0


def _normalize_node(raw: Mapping[str, Any], index: int, coord_div: float) -> dict[str, Any]:
    raw_frame = raw.get("frame")
    if not isinstance(raw_frame, list) or len(raw_frame) != 4 or not all(_is_finite_number(v) for v in raw_frame):
        raise _unavailable(f"guest_axruntime: invalid frame on node {index}: {raw_frame!r}")
    fx, fy, fw, fh = (float(v) / coord_div for v in raw_frame)

    traits_out: list[str] = []
    seen_traits: set[str] = set()
    for trait in raw.get("traits") or []:
        t_str = str(trait or "").strip()
        if t_str and t_str not in seen_traits:
            seen_traits.add(t_str)
            traits_out.append(t_str)

    raw_val = raw.get("value")
    norm_val = None if raw_val is None else str(raw_val)

    is_visible = raw.get("is_visible")
    hit_test = raw.get("hit_test")
    if fw <= 0 or fh <= 0 or is_visible is False:
        hittable = False
        occluded_reason = "offscreen_or_empty"
    elif isinstance(hit_test, Mapping) and hit_test.get("matches_self") is False:
        hittable = False
        occluded_reason = "hit_test_occluded"
    else:
        hittable = True
        occluded_reason = ""

    node: dict[str, Any] = {
        "path": f"0/{index}",
        "label": str(raw.get("label") or ""),
        "role": str(raw.get("role") or "").strip() or "element",
        "traits": traits_out,
        "enabled": bool(raw.get("enabled", True)),
        "value": norm_val,
        "identifier": str(raw.get("identifier") or ""),
        "frame": [fx, fy, fw, fh],
        "hittable": hittable,
    }
    if occluded_reason:
        node["occluded_reason"] = occluded_reason
    return node


def normalize_probe_to_ax_v1(
    probe_raw: Mapping[str, Any],
    *,
    app: str,
    request_id: str,
    pid: int | None = None,
    short_version: str | None = None,
    bundle_version: str | None = None,
    capabilities_screen: Mapping[str, Any] | None = None,
    captured_at_ms: int | None = None,
) -> dict[str, Any]:
    """Normalize a ``guest_ax_probe`` JSON payload to canonical ``autophone.ax/1``."""
    if probe_raw.get("schema") != PROBE_SCHEMA:
        raise _unavailable(f"unexpected guest_ax_probe schema {probe_raw.get('schema')!r}")

    if _voiceover_enabled(probe_raw.get("ax_state")):
        raise _unavailable("guest_axruntime: VoiceOver unexpectedly enabled; refusing AX snapshot")

    probe_bundle = str(probe_raw.get("bundle_id") or "").strip()
    if probe_bundle and probe_bundle != app:
        raise _unavailable(f"guest_axruntime: bundle_id mismatch got={probe_bundle!r} want={app!r}")

    effective_pid = _positive_int(pid) or _positive_int(probe_raw.get("pid")) or 0
    system = dict(probe_raw.get("system") or {}) if isinstance(probe_raw.get("system"), Mapping) else {}
    foreground_active, occlusion_reason = _evaluate_foreground_gate(app, effective_pid or None, system)

    probe_screen = probe_raw.get("screen")
    if not isinstance(probe_screen, Mapping):
        raise _unavailable("guest_axruntime: probe output missing screen descriptor")
    out_w, out_h, out_scale, coord_div = _resolve_screen_geometry(
        probe_screen,
        capabilities_screen,
        foreground_active=foreground_active,
    )

    raw_nodes = probe_raw.get("nodes")
    if not isinstance(raw_nodes, list):
        raise _unavailable("guest_axruntime: probe output missing nodes list")

    retries = int(probe_raw.get("first_wake_retries") or 0)
    if foreground_active:
        if not raw_nodes:
            raise _unavailable(f"guest_axruntime: empty AX tree after {retries} first-wake retries")
        normalized_nodes = [
            _normalize_node(node, idx, coord_div)
            for idx, node in enumerate(raw_nodes)
            if isinstance(node, Mapping)
        ]
        if len(normalized_nodes) != len(raw_nodes):
            raise _unavailable("guest_axruntime: non-object node entry in probe output")
    else:
        normalized_nodes = []

    raw_ts = captured_at_ms if captured_at_ms is not None else probe_raw.get("captured_at_ms")
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, int) or raw_ts <= 0:
        ts = int(time.time() * 1000)
    else:
        ts = raw_ts

    resolved_short = str(short_version if short_version is not None else (probe_raw.get("short_version") or "")).strip()
    resolved_bundle = str(bundle_version if bundle_version is not None else (probe_raw.get("bundle_version") or "")).strip()

    if isinstance(probe_screen.get("app_frame"), list) and len(probe_screen["app_frame"]) == 4:
        system.setdefault("app_frame", list(probe_screen["app_frame"]))

    out: dict[str, Any] = {
        "schema": "autophone.ax/1",
        "evidence_protocol": 1,
        "snapshot_kind": "ui_structure",
        "ax_backend": "guest_axruntime",
        "request_id": request_id,
        "captured_at_ms": ts,
        "app": app,
        "pid": effective_pid,
        "complete": bool(probe_raw.get("complete", True)),
        "truncated": bool(probe_raw.get("truncated", False)),
        "short_version": resolved_short,
        "bundle_version": resolved_bundle,
        "foreground_active": foreground_active,
        "foreground_occlusion_reason": occlusion_reason,
        "first_wake_retries": retries,
        "screen": {"scale": out_scale, "w": out_w, "h": out_h},
        "system": system,
        "nodes": normalized_nodes,
    }
    if not foreground_active:
        out["suppressed_background_node_count"] = len(raw_nodes)
    return out


__all__ = [
    "BUILD_SCHEMA",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_DELAY_MS",
    "GUEST_DIR",
    "GUEST_PREFIX",
    "GuestAxProbeRunner",
    "LocalGuestAxProbe",
    "PROBE_ENV",
    "PROBE_MIN_VERSION",
    "PROBE_SCHEMA",
    "forget_probe_deployments",
    "normalize_probe_to_ax_v1",
    "resolve_local_probe",
]
