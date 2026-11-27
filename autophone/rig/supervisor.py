"""In-lock VM/App capability probes and one bounded recovery ladder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


Probe = Callable[[], dict[str, Any]]
ForegroundProbe = Callable[[Any], dict[str, Any]]
# A recovery rung is told what it is recovering from: whether a rung can help
# depends on the failure's shape, and a rung that cannot help must not run.
FailureRecover = Callable[[Any, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RigSnapshot:
    vm_uds: dict[str, Any]
    app_ui: dict[str, Any]
    frida: dict[str, Any]
    health_guard: dict[str, Any]

    def availability(self) -> dict[str, bool]:
        return {
            "vm_uds": bool(self.vm_uds.get("available")),
            "app_ui": bool(self.app_ui.get("available")),
            "frida": bool(self.frida.get("available")),
        }

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Return a JSON-safe snapshot for the run result and artifact file."""
        return {
            "vm_uds": dict(self.vm_uds),
            "app_ui": dict(self.app_ui),
            "frida": dict(self.frida),
            "health_guard": dict(self.health_guard),
        }

    def recovery_advice(self) -> str:
        if self.health_guard.get("on_down") and self.health_guard.get("state") == "recovering":
            return "等待/观察守护恢复，再重新探测 VM 状态"
        if not self.vm_uds.get("available"):
            return "显式执行 scripts/start_vphone.sh 后重新运行"
        if not self.app_ui.get("available"):
            return str(self.app_ui.get("recovery") or "检查 App 前台化与 pid/ready 证据后重试")
        return "无需基础设施恢复"


class RigSupervisor:
    def __init__(
        self,
        *,
        uds_probe: Probe,
        foreground_probe: ForegroundProbe,
        foreground_recover: ForegroundProbe,
        frida_probe: Probe,
        frida_recover: FailureRecover,
        health_guard_probe: Probe,
        prepare_app: Callable[[], str | None] | None = None,
    ) -> None:
        self._uds_probe = uds_probe
        self._foreground_probe = foreground_probe
        self._foreground_recover = foreground_recover
        self._frida_probe = frida_probe
        self._frida_recover = frida_recover
        self._health_guard_probe = health_guard_probe
        self._prepare_app = prepare_app

    def prepare(self, profile: Any, *, need_app: bool, need_frida: bool) -> RigSnapshot:
        """Probe each capability, then walk its one bounded recovery rung.

        Frida gets a rung for the same reason ``app_ui`` does: the common
        failure is target-side, not a tunnel or version fault.  A wedged app
        process accepts ``attach`` and then hangs every ``script.load()``, which
        reads as "no Frida" even though the daemon is healthy — and it silently
        disables every capability derived from injection
        (``location_injection``, ``api_capture``).  Probing once and reporting
        the corpse turned that one-command fix into a whole blocked fulfillment
        branch (run 20260727-225535).

        The rung is the recoverer's decision, not this loop's: it is handed the
        failure and may decline.  That matters because the rung restarts the app,
        so firing it on a failure it cannot fix *destroys* a working process
        (run 20260728-075026 did exactly that).  Whenever it does act, ``app_ui``
        is re-probed — a rung that killed the app and could not bring it back
        must not leave a snapshot still claiming the app is up.
        """
        vm_uds = dict(self._uds_probe())
        health_guard = dict(self._health_guard_probe())
        app_ui = {"available": not need_app, "evidence": "not_required"}
        if need_app and vm_uds.get("available") and self._prepare_app is not None:
            preparation_error = self._prepare_app()
            if preparation_error:
                # The Run already owns ScreenLock. Do not relaunch or inject
                # after its bounded screen preparation failed.
                app_ui = {"available": False, "evidence": preparation_error,
                          "recovery": preparation_error}
                return RigSnapshot(
                    vm_uds=vm_uds, app_ui=app_ui,
                    frida={"available": not need_frida,
                           "evidence": "screen_preparation_blocked" if need_frida else "not_required"},
                    health_guard=health_guard,
                )
        if need_app and vm_uds.get("available"):
            app_ui = dict(self._foreground_probe(profile))
            if not app_ui.get("available"):
                app_ui = dict(self._foreground_recover(profile))
        frida = {"available": not need_frida, "evidence": "not_required"}
        if need_frida and vm_uds.get("available"):
            frida = dict(self._frida_probe())
            if not frida.get("available"):
                # Carry the pre-recovery failure forward: "recovered after a
                # relaunch" and "was fine all along" must not read alike, or the
                # rig snapshot stops being usable evidence about flakiness.
                first_evidence = str(frida.get("evidence") or "")
                recovered = dict(self._frida_recover(profile, frida))
                if recovered.get("recovery_attempted"):
                    recovered["evidence"] = (
                        f"{recovered.get('evidence')}; first probe: {first_evidence}")
                    if need_app:
                        app_ui = dict(self._foreground_probe(profile))
                frida = recovered
        return RigSnapshot(vm_uds=vm_uds, app_ui=app_ui, frida=frida, health_guard=health_guard)
