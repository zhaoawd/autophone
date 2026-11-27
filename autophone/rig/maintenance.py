"""Rig lifecycle preparation over DeviceSession and bounded recovery policy."""
from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.assets.apps import AppProfile
from autophone.device import DeviceProtocolError, DeviceSession
from autophone.observation.capture import ax_scope
from autophone.rig.supervisor import RigSnapshot, RigSupervisor


@dataclass(frozen=True)
class RigStateSnapshot:
    host_id: str
    rig_id: str
    rig_generation: int
    capability_support: tuple[str, ...]
    capability_state: Mapping[str, Any]
    rig: RigSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "rig_id": self.rig_id,
            "rig_generation": self.rig_generation,
            "capability_support": list(self.capability_support),
            "capability_state": dict(self.capability_state),
            **self.rig.to_dict(),
        }


logger = logging.getLogger(__name__)

AppProbe = Callable[[DeviceSession, Any], dict[str, Any]]
AppRecover = Callable[[DeviceSession, Any], dict[str, Any]]
FridaProbe = Callable[[DeviceSession, Any], dict[str, Any]]
FridaRecover = Callable[[DeviceSession, Any, dict[str, Any]], dict[str, Any]]
HealthProbe = Callable[[DeviceSession], dict[str, Any]]


def _app_probe(session: DeviceSession, profile: Any) -> dict[str, Any]:
    row = next(
        (
            item for item in session.app_list()
            if str(item.get("bundle_id") or item.get("bundleIdentifier") or "")
            == str(profile.bundle_id)
        ),
        None,
    )
    return {
        "available": bool(row and int(row.get("pid") or 0) > 0),
        "evidence": f"app_list pid={int((row or {}).get('pid') or 0)}",
    }


def _app_recover(session: DeviceSession, profile: Any) -> dict[str, Any]:
    bundle_id = str(profile.bundle_id)
    launch_path = "native app_launch"
    native_error = ""
    try:
        session.launch_app(bundle_id)
    except DeviceProtocolError as exc:
        native_error = str(exc)
        launch_path = "uiopen fallback"
        try:
            result = session.shell(
                f"uiopen --bundleid {shlex.quote(bundle_id)}",
                write=True,
                timeout_ms=15_000,
            )
        except Exception as fallback_exc:  # noqa: BLE001 - return a probe verdict
            return {
                "available": False,
                "evidence": (
                    f"native app_launch failed: {native_error}; "
                    f"uiopen failed: {type(fallback_exc).__name__}: {fallback_exc}"
                ),
            }
        if int(getattr(result, "code", 1)) != 0:
            return {
                "available": False,
                "evidence": (
                    f"native app_launch failed: {native_error}; "
                    f"uiopen exit={getattr(result, 'code', None)}"
                ),
            }

    capture = (((getattr(profile, "raw", {}) or {}).get("capabilities") or {})
               .get("api_capture") or {})
    try:
        timeout_s = max(0.0, float(capture.get("ready_timeout_s", 15.0)))
    except (TypeError, ValueError):
        timeout_s = 15.0
    deadline = time.monotonic() + timeout_s
    while True:
        probe = _app_probe(session, profile)
        if probe["available"]:
            probe["evidence"] = f"{probe['evidence']}; launch={launch_path}"
            return probe
        if time.monotonic() >= deadline:
            detail = f"{probe['evidence']}; launch={launch_path}; timeout={timeout_s:g}s"
            if native_error:
                detail += f"; native_error={native_error}"
            return {"available": False, "evidence": detail}
        time.sleep(0.2)


def _frida_probe(session: DeviceSession, profile: Any) -> dict[str, Any]:
    result = session.frida_probe(str(getattr(profile, "display_name", "") or profile.app))
    return {
        "available": result.get("ok") is True,
        "evidence": f"stage={result.get('stage')}; endpoint={session.target.frida_endpoint}",
        "frida": result,
    }


def _frida_recover(
    session: DeviceSession,
    profile: Any,
    failure: dict[str, Any],
) -> dict[str, Any]:
    return dict(failure)


def _health_probe(session: DeviceSession) -> dict[str, Any]:
    return {
        "state": "target_bound",
        "daemon": False,
        "on_down": False,
        "evidence": f"rig={session.target.rig_id}",
    }


class RigMaintenance:
    def __init__(
        self,
        session: DeviceSession | None,
        *,
        capability_support: list[str],
        app_probe: AppProbe = _app_probe,
        app_recover: AppRecover = _app_recover,
        frida_probe: FridaProbe = _frida_probe,
        frida_recover: FridaRecover = _frida_recover,
        health_probe: HealthProbe = _health_probe,
        run_launch_app: Callable[[Any, Path], str | None] | None = None,
        lock_state_probe: Callable[[], tuple[bool | None, str]] | None = None,
    ) -> None:
        self.session = session
        self._launch_app = run_launch_app
        # 原生锁屏状态（notify lockstate，guest 辅助程序读取）；None 时用 vphone_native_state.native_lock_state。
        self._lock_state_probe = lock_state_probe
        # 本次 prepare_run 内最近一次启动的失败说明；None 表示尚未启动或启动成功。
        self._launch_attempted = False
        self._last_launch_error: str | None = None
        self.capability_support = tuple(sorted(set(capability_support)))
        self.app_probe = app_probe
        self.app_recover = app_recover
        self.frida_probe = frida_probe
        self.frida_recover = frida_recover
        self.health_probe = health_probe

    def prepare(
        self,
        profile: Any,
        *,
        need_app: bool,
        need_frida: bool,
    ) -> RigStateSnapshot:
        if self.session is None:
            raise RuntimeError("Assignment preparation requires a DeviceSession")
        supervisor = RigSupervisor(
            uds_probe=self.session.probe_uds,
            foreground_probe=lambda item: self.app_probe(self.session, item),
            foreground_recover=lambda item: self.app_recover(self.session, item),
            frida_probe=lambda: self.frida_probe(self.session, profile),
            frida_recover=lambda item, failure: self.frida_recover(
                self.session, item, failure,
            ),
            health_guard_probe=lambda: self.health_probe(self.session),
        )
        rig = supervisor.prepare(profile, need_app=need_app, need_frida=need_frida)
        return RigStateSnapshot(
            host_id=self.session.target.host_id,
            rig_id=self.session.target.rig_id,
            rig_generation=self.session.target.rig_generation,
            capability_support=self.capability_support,
            capability_state=rig.availability(),
            rig=rig,
        )

    def prepare_run(
        self,
        profile: AppProfile,
        shot_dir: Path,
        *,
        need_app: bool,
        need_frida: bool,
        prepare_app: Callable[[], str | None] | None = None,
    ) -> Any:
        """Probe/recover the shared rig while the regression already owns ScreenLock."""
        from autophone.rig.supervisor import RigSupervisor

        if self._launch_app is None:
            raise RuntimeError("Run preparation requires a launch_app operation")
        self._launch_attempted = False
        self._last_launch_error = None
        supervisor = RigSupervisor(
            uds_probe=self._probe_vm_uds,
            foreground_probe=lambda item: self._probe_app_ui(item, shot_dir),
            foreground_recover=lambda item: self._recover_app_ui(item, shot_dir),
            frida_probe=lambda: self._probe_frida(profile),
            frida_recover=lambda item, failure: self._recover_frida(item, failure, shot_dir),
            health_guard_probe=self._probe_health_guard,
            prepare_app=prepare_app,
        )
        return supervisor.prepare(profile, need_app=need_app, need_frida=need_frida)
    _FRIDA_RECOVERABLE_STAGES = frozenset({
        "ATTACH_TIMEOUT", "RPC_TIMEOUT", "SCRIPT_LOAD_TIMEOUT", "SCRIPT_LOAD_FAILED",
        "CONFIGURE_TIMEOUT", "PROBE_TIMEOUT", "WORKER_EXITED", "APP_NOT_ATTACHABLE",
    })

    # 有状态 App 的 bundle_id：恢复时不重启这些 App。公开版没有登记项。
    _STATEFUL_RECOVERY_PROTECTED_BUNDLES: frozenset[str] = frozenset()

    @staticmethod
    def _probe_vm_uds() -> dict[str, Any]:
        """Same three-state UDS verdict the Rig path uses (``probe_uds_state``).

        This used to send ``ping`` and treat any parsable reply as available,
        which is why the compatibility path could call the device online at the
        same instant Assignment preflight called it unavailable.
        """
        from autophone.device import probe_uds_state
        from autophone.execution.engine.actions import resolve_sock

        try:
            sock = resolve_sock()
        except Exception as exc:  # noqa: BLE001 - turn target resolution into a blocker
            return {
                "available": False,
                "state": "unreachable",
                "evidence": f"uds target unresolved: {type(exc).__name__}: {exc}",
            }
        return probe_uds_state(sock, timeout=3.0)

    def _probe_app_ui(self, profile: AppProfile, shot_dir: Path) -> dict[str, Any]:
        """Require pid plus a visible ready marker; never trust SBS foreground readout.

        OCR is the normal proof.  An App profile may explicitly enable AX because
        compact Chinese menu text can be invisible to the OCR backend; when OCR has no
        text at all, a fresh AX tree is an equally direct in-app visibility proof.
        AX remains a fallback only, and still needs a live pid plus one of the
        profile-owned ready markers.

        The AX fallback runs only after this preparation launched the app.  Before
        that, a missing marker can mean another app or SpringBoard is in front: the
        app under test is then a background process, a Frida attach to it waits for
        Frida's own timeout (about 25 s on vm, BL-226), and an AX tree read
        from a background process would not prove the app is visible.  The
        recovery rung launches the app and probes again with the fallback allowed.
        """
        return self._probe_app_ui_detail(profile, shot_dir)[0]

    def _probe_app_ui_detail(self, profile: AppProfile, shot_dir: Path) -> tuple[dict[str, Any], int]:
        """``_probe_app_ui`` plus the pid it read (0 when unknown or the probe failed)."""
        from autophone.observation.capture import _ready_markers
        from autophone.execution.engine.actions import CaseContext

        try:
            ctx = CaseContext(case_id="_rig_probe", shot_dir=shot_dir / "_rig_probe", timeout=5.0)
            pid = ctx._await_app_pid(profile.bundle_id, deadline_s=0.0)
            marker = ctx._await_app_ready_marker(_ready_markers(profile), deadline_s=2.0)
        except Exception as exc:  # noqa: BLE001
            return self._explain_screen_state({
                "available": False, "evidence": f"pid/ready probe failed: {type(exc).__name__}: {exc}"}), 0
        source = "ocr"
        if not marker and self._launch_attempted:
            marker = self._probe_ax_ready_marker(profile, _ready_markers(profile), shot_dir.parent)
            source = "ax" if marker else source
        if pid > 0 and marker:
            return {"available": True, "evidence": f"pid={pid}; ready_marker={marker!r}; source={source}"}, pid
        return self._explain_screen_state({
            "available": False,
            "evidence": f"pid={pid}; ready_marker={marker!r} (SBS foreground is not accepted as evidence)",
        }), pid

    def _run_launch(self, profile: AppProfile, shot_dir: Path) -> str | None:
        if self._launch_app is None:
            raise RuntimeError("Run preparation requires a launch_app operation")
        self._launch_attempted = True
        error = self._launch_app(profile, shot_dir)
        self._last_launch_error = str(error) if error else None
        return error

    def _explain_screen_state(self, result: dict[str, Any]) -> dict[str, Any]:
        """After a launch attempt left App UI unavailable, name a locked or frozen screen.

        Locked/off displays make UDS screenshots repeat the last frame, so the
        generic pid/ready advice hid the cause (run 20261005-164238). Only runs
        after this preparation already tried a launch, keeping the native read
        off the ordinary "app not running yet" path.
        """
        if not self._launch_attempted:
            return result
        from autophone.execution.engine.actions import STALE_SCREEN_MARK

        probe = self._lock_state_probe
        if probe is None:
            from autophone.observation.vphone_native_state import native_lock_state as probe
        try:
            locked, lock_evidence = probe()
        except Exception as exc:  # noqa: BLE001 - explanation only, verdict unchanged
            locked, lock_evidence = None, f"native lock state unavailable: {type(exc).__name__}: {exc}"
        out = dict(result)
        if locked is True:
            out["evidence"] = f"{out.get('evidence')}; {lock_evidence}"
            out["recovery"] = (
                "设备处于锁屏（原生锁屏状态为锁屏）：锁屏或熄屏期间 UDS 截图可能停在旧帧，"
                "App 无法前台化。按 Home 解锁并排查锁屏原因后重试"
            )
        elif STALE_SCREEN_MARK in (self._last_launch_error or ""):
            out["recovery"] = (
                "截图未刷新：启动后就绪标记只出现在与启动前字节相同的帧上，设备可能熄屏或锁屏"
                f"（原生锁屏状态未读到：{lock_evidence}）。确认屏幕状态并解锁后重试"
            )
        return out

    @staticmethod
    def _probe_ax_ready_marker(profile: AppProfile, markers: list[str], run_dir: Path) -> str:
        """Read one fresh AX tree as the ready evidence fallback (no tap/mutation)."""
        try:
            from autophone.observation.capture import build_run_ax_session
            from autophone.device.ax_target import find_node

            with ax_scope(build_run_ax_session(profile, run_dir)) as ax_provider:
                if ax_provider is None:
                    return ""
                dump = ax_provider.refresh()
                for marker in markers:
                    if find_node(dump, marker, enabled_only=False):
                        return marker
        except Exception as exc:  # noqa: BLE001 - OCR result remains the conservative fallback
            logger.debug("AX ready marker probe unavailable: %s", exc)
        return ""

    def _recover_app_ui(self, profile: AppProfile, shot_dir: Path) -> dict[str, Any]:
        launch_error = self._run_launch(profile, shot_dir)
        if launch_error:
            return self._explain_screen_state({"available": False, "evidence": launch_error})
        probed, pid = self._probe_app_ui_detail(profile, shot_dir)
        if probed.get("available") or pid <= 0:
            return probed
        return self._reset_home_and_probe(profile, shot_dir, probed)

    def _home_reset_skip_reason(self, profile: AppProfile, probed: dict[str, Any]) -> str:
        if probed.get("recovery"):
            # Locked or frozen screen: navigation taps cannot reach the app.
            return "screen_locked_or_stale"
        if (
            str(getattr(profile, "bundle_id", "") or "") in self._STATEFUL_RECOVERY_PROTECTED_BUNDLES
            and os.environ.get("AUTOPHONE_NO_ORDER_MAINTENANCE", "") != "1"
        ):
            # Same guard as the Frida rung: reset_to_home cold-kills an in-progress stateful session.
            return "stateful_app_protected"
        return ""

    @staticmethod
    def _profile_reset_policy(profile: Any) -> str:
        value = getattr(profile, "reset", None)
        if value is None:
            raw = getattr(profile, "raw", None)
            if isinstance(raw, Mapping):
                value = raw.get("reset")
        return str(value or "").strip().lower()

    @staticmethod
    def _is_unregistered_home_reset(reset: Mapping[str, Any]) -> bool:
        if "registered" in reset:
            return reset.get("registered") is False
        if reset.get("ok"):
            return False
        return "unknown operation 'reset_to_home'" in str(reset.get("error") or "")

    def _reset_home_and_probe(
        self, profile: AppProfile, shot_dir: Path, probed: dict[str, Any],
    ) -> dict[str, Any]:
        """Reset a running non-home app back to Home once and re-probe readiness.

        The launch rung only foregrounds a running process, so a sub-page left by the
        previous Run's last Case stays in front and carries no profile ready marker
        (especially under zero-Frida runs where post-run terminate is skipped).
        This rung runs only after this preparation launched the app, so it never acts
        on a background process.

        Resolution order (deterministic, no AI decision):
        1. Dispatch the App's registered ``reset_to_home`` Operation if present.
        2. When no ``reset_to_home`` Operation is registered and the profile declares
           ``reset: relaunch``, fall back to cold-relaunching the app via
           ``_relaunch_app`` (terminate + launch) and re-probing readiness.
        3. Otherwise preserve the existing ``reset_to_home`` verdict without an
           unexpected cold kill.

        The reset and its result stay in ``home_reset``.  The verdict is the probe after
        the reset: a failed or ineffective reset leaves App UI unavailable, and the
        evidence still starts with that probe's pid/ready-marker result.
        """
        skip = self._home_reset_skip_reason(profile, probed)
        if skip:
            logger.info("App UI 未就绪（%s），跳过 reset_to_home：%s", probed.get("evidence"), skip)
            return {**probed, "home_reset": {"operation": "reset_to_home", "attempted": False, "skipped": skip}}
        logger.info("App UI 未就绪（%s），pid 存活，执行 reset_to_home 后复探", probed.get("evidence"))
        from autophone.observation.capture import dispatch_reset_to_home

        reset = dispatch_reset_to_home(
            shot_dir / "_rig_home_reset", profile, case_id="_rig_home_reset")
        if (
            self._is_unregistered_home_reset(reset)
            and self._profile_reset_policy(profile) == "relaunch"
        ):
            return self._relaunch_home_and_probe(profile, shot_dir, probed, reset)
        if not reset.get("ok"):
            logger.warning("Run 准备阶段 reset_to_home 失败（%s），按复探结果判定", reset.get("error"))
        after, _pid = self._probe_app_ui_detail(profile, shot_dir)
        status = "ok" if reset.get("ok") else f"failed: {reset.get('error')}"
        out = dict(after)
        out["evidence"] = (
            f"{after.get('evidence')}; home_reset=reset_to_home {status}; "
            f"before reset: {probed.get('evidence')}"
        )
        out["home_reset"] = {**reset, "attempted": True, "probe_before": str(probed.get("evidence") or "")}
        return out

    def _relaunch_home_and_probe(
        self,
        profile: AppProfile,
        shot_dir: Path,
        probed: dict[str, Any],
        reset_to_home_outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Cold-relaunch when no ``reset_to_home`` op exists and ``profile.reset == 'relaunch'``."""
        logger.info(
            "App UI 未就绪（%s），未注册 reset_to_home 且 profile.reset=relaunch，冷重启 %s 后复探",
            probed.get("evidence"),
            profile.bundle_id,
        )
        started = time.monotonic()
        relaunch_error = self._relaunch_app(profile, shot_dir)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        home_reset: dict[str, Any] = {
            "operation": "relaunch_app",
            "strategy": "profile_reset_relaunch",
            "fallback_from": "reset_to_home",
            "fallback_reason": "reset_to_home_unregistered",
            "ok": relaunch_error is None,
            "elapsed_ms": elapsed_ms,
            "attempted": True,
            "probe_before": str(probed.get("evidence") or ""),
        }
        if reset_to_home_outcome.get("error"):
            home_reset["reset_to_home_error"] = str(reset_to_home_outcome.get("error"))
        if relaunch_error is not None:
            home_reset["error"] = str(relaunch_error)
            logger.warning("Run 准备阶段 profile.reset=relaunch 冷重启失败（%s）", relaunch_error)
            failed = self._explain_screen_state({
                "available": False,
                "evidence": (
                    f"relaunch_app failed: {relaunch_error}; "
                    f"home_reset=relaunch_app failed: {relaunch_error}; "
                    f"before reset: {probed.get('evidence')}"
                ),
            })
            failed["home_reset"] = home_reset
            return failed

        after, _pid = self._probe_app_ui_detail(profile, shot_dir)
        out = dict(after)
        out["evidence"] = (
            f"{after.get('evidence')}; home_reset=relaunch_app ok; "
            f"before reset: {probed.get('evidence')}"
        )
        out["home_reset"] = home_reset
        return out

    @staticmethod
    def _probe_frida(profile: AppProfile) -> dict[str, Any]:
        try:
            from autophone.device.frida_process import default_frida_host
            from autophone.device.frida import probe_contract

            host = default_frida_host()
            result = probe_contract(
                host=host,
                app_name=str(getattr(profile, "display_name", "") or getattr(profile, "app", "")),
                timeout_s=float(os.environ.get("AP_FRIDA_PROBE_TIMEOUT", "8.0")),
                bundle_id=str(getattr(profile, "bundle_id", "") or "") or None,
            )
            if result.get("ok"):
                return {
                    "available": True,
                    "evidence": f"frida contract ready {host}; stage={result.get('stage')}; "
                                f"elapsed_ms={result.get('elapsed_ms')}",
                    "frida": result,
                }
            return {
                "available": False,
                "evidence": f"frida unavailable stage={result.get('stage')}: {result.get('detail')}",
                "frida": result,
            }
        except Exception as exc:  # noqa: BLE001 - Frida is optional for ordinary UI coverage
            return {"available": False, "evidence": f"frida unavailable: {type(exc).__name__}: {exc}"}

    def _recover_frida(self, profile: AppProfile, failure: dict[str, Any],
                       shot_dir: Path) -> dict[str, Any]:
        """Cold-relaunch the injection target once, then re-probe.

        The probe target is the app under test, so the app process — not the
        daemon — is the part of the injection chain a run can repair itself.

        Declining is as important as acting.  ``DYLD_PROBE_FAILED`` is an
        environment-level dyld-introspection incompatibility that a new process
        inherits unchanged, so relaunching on it only trades a degraded rig for
        a dead one: run 20260728-075026 killed a healthy app over that error and
        then could not restart it, blocking the whole run.  Recover only from
        the timeout/hang shapes a stale process actually produces.
        """
        stage = str((failure.get("frida") or {}).get("stage") or "")
        if stage not in self._FRIDA_RECOVERABLE_STAGES:
            logger.info(
                "frida 探测失败 stage=%s 不属于冷重启可修类型，保留原判不动 App", stage or "unknown")
            return dict(failure)
        bundle_id = str(getattr(profile, "bundle_id", "") or "")
        if (
            bundle_id in self._STATEFUL_RECOVERY_PROTECTED_BUNDLES
            and os.environ.get("AUTOPHONE_NO_ORDER_MAINTENANCE", "") != "1"
        ):
            protected = dict(failure)
            protected["recovery_blocked"] = "stateful_app_protected"
            first_evidence = str(protected.get("evidence") or "")
            protected["evidence"] = (
                f"{first_evidence}; " if first_evidence else ""
            ) + (
                "automatic app relaunch blocked: set "
                "AUTOPHONE_NO_ORDER_MAINTENANCE=1 only in an explicit "
                "no-order maintenance run"
            )
            logger.warning(
                "frida 探测失败 stage=%s，但 %s 是订单状态载体；未声明无订单维护模式，"
                "拒绝自动冷重启",
                stage,
                bundle_id,
            )
            return protected
        logger.warning("frida 探测失败 stage=%s，冷重启被测 App %s 后复探", stage, profile.bundle_id)
        relaunch_error = self._relaunch_app(profile, shot_dir)
        if relaunch_error:
            return {
                "available": False,
                "evidence": f"frida recovery relaunch failed: {relaunch_error}",
                "recovery_attempted": "app_relaunch",
            }
        recovered = self._probe_frida(profile)
        recovered["recovery_attempted"] = "app_relaunch"
        return recovered

    def _relaunch_app(self, profile: AppProfile, shot_dir: Path) -> str | None:
        """Terminate then launch, so the next attach targets a fresh process."""
        if self._launch_app is None:
            raise RuntimeError("Run preparation requires a launch_app operation")
        from autophone.execution.engine.actions import CaseContext, StepFailure

        ctx = CaseContext(case_id="_platform_relaunch", shot_dir=shot_dir, timeout=15.0)
        logger.info("冷重启被测 App %s", profile.bundle_id)
        try:
            ctx.terminate_app(profile.bundle_id)
        except StepFailure as exc:
            logger.error("App 终止失败：%s", exc.result.detail)
            return f"app terminate failed: {exc.result.detail}"
        except Exception as exc:  # noqa: BLE001 - terminate failure is recovery evidence
            logger.error("App 终止失败：%s: %s", type(exc).__name__, exc)
            return f"app terminate failed: {type(exc).__name__}: {exc}"
        return self._run_launch(profile, shot_dir)

    @staticmethod
    def _probe_health_guard() -> dict[str, Any]:
        try:
            from autophone.device.vsock_health_client import peek_state, resolve_state_file

            health = peek_state()
            present = resolve_state_file().exists() and health.state not in {"missing", "stale"}
            state = "recovering" if present and health.state == "down" else health.state
            return {
                "state": state,
                "daemon": present,
                # The standard start script starts this daemon with --on-down;
                # retain the state-file evidence even if process inspection is unavailable.
                "on_down": present,
                "last_error": health.last_error,
            }
        except Exception as exc:  # noqa: BLE001
            return {"state": "unknown", "daemon": False, "on_down": False, "error": str(exc)}
