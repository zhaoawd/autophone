"""Package installation orchestration shared by HTTP and CLI adapters."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Mapping, Protocol

from autophone.application.control_plane import (
    ControlPlaneRequiredForRigTarget,
    TargetRigPinRefused,
    pin_target_rig,
)


OBSERVATION_SCHEMA_VERSION = "installed-build-observation/v1"


class PackageStore(Protocol):
    """Build Catalog + Device Control 的最小面：解析完整 Build Ref、按 ref 精确安装、
    给出当前安装目标的 canonical 摘要。省略 slot 与 build_id 时由 Build Catalog 按
    ``default_slot`` 解析（BL-011）。"""

    def resolve_build_ref(
        self, app: str, *, slot: str | None = None, build_id: str | None = None,
    ) -> Any: ...

    def install_build_ref(self, build_ref: Any, *, verify: bool) -> dict[str, Any]: ...

    def install_target_fingerprint(self) -> str: ...


class ObservedStateStore(Protocol):
    @property
    def identity(self) -> Any: ...

    def snapshot(self) -> dict[str, Any]: ...

    def installed_build(self, app: str) -> dict[str, Any] | None: ...

    def publish_installed_build(
        self,
        build: Mapping[str, Any] | None,
    ) -> dict[str, Any]: ...


ControlProvider = Callable[[], Any | None]


class PackageInstallApplication:
    """Choose Assignment or compatibility execution and publish one observed fact."""

    def __init__(
        self,
        package_store: PackageStore,
        control_provider: ControlProvider,
        local_observed_state: ObservedStateStore,
    ) -> None:
        self.package_store = package_store
        self.control_provider = control_provider
        self.local_observed_state = local_observed_state

    def observed_build(
        self,
        app: str,
        *,
        rig_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        control = self.control_provider()
        if control is None:
            return (
                str(self.local_observed_state.identity.rig_id),
                self.local_observed_state.installed_build(app),
            )
        rigs = control.list_rigs()
        selected = next((row for row in rigs if row.get("rig_id") == rig_id), None)
        if selected is None and rig_id is None and len(rigs) == 1:
            selected = rigs[0]
        if selected is None:
            return None, None
        observed = dict(selected.get("observed_state") or {})
        candidate = observed.get("installed_build")
        installed = (
            dict(candidate)
            if isinstance(candidate, Mapping) and candidate.get("app") == app
            else None
        )
        return str(selected.get("rig_id") or "") or None, installed

    def installed_build_observation(
        self,
        app: str,
        *,
        rig_id: str | None = None,
    ) -> dict[str, Any] | None:
        """权威安装事实文档（``installed-build-observation/v1``），没有观测时 None。

        来源二选一：控制面 ``rigs.observed_state.installed_build``（Provisioning 回读后
        发布），或单 VM 兼容路径的 ``FileRigObservedState``（本类 ``install`` 在验证成功后
        发布）。``target_fingerprint`` 只有观测来自本机 Rig 时才能给出；其它 Rig 的观测
        带 ``None``，准入侧据此 fail-closed。安装回执（``install-receipt.json`` /
        ``installations/*.json``）不在这里读：它们是兼容与审计数据（目标架构 §12.1）。
        """
        control = self.control_provider()
        local_identity = self.local_observed_state.identity
        if control is None:
            snapshot = self.local_observed_state.snapshot()
            installed = snapshot.get("observed_state", {}).get("installed_build")
            if not isinstance(installed, Mapping) or installed.get("app") != app:
                return None
            return self._observation(
                app,
                source="rig_observed_state",
                host_id=str(snapshot.get("host_id") or local_identity.host_id),
                rig_id=str(snapshot.get("rig_id") or local_identity.rig_id),
                rig_generation=int(snapshot.get("rig_generation") or local_identity.rig_generation),
                installed_build=installed,
                observed_at=str(snapshot.get("updated_at") or ""),
                local=True,
            )
        rigs = control.list_rigs()
        selected = next((row for row in rigs if row.get("rig_id") == rig_id), None)
        if selected is None and rig_id is None:
            selected = next(
                (row for row in rigs if row.get("rig_id") == str(local_identity.rig_id)), None,
            )
            if selected is None and len(rigs) == 1:
                selected = rigs[0]
        if selected is None:
            return None
        installed = dict(selected.get("observed_state") or {}).get("installed_build")
        if not isinstance(installed, Mapping) or installed.get("app") != app:
            return None
        # 安装时间属于安装事实；Rig 的 last_seen 会因重新握手改变，不能用于证据身份。
        installed_at = str(installed.get("installed_at") or "")
        if not installed_at:
            return None
        selected_rig = str(selected.get("rig_id") or "")
        return self._observation(
            app,
            source="control_plane",
            host_id=str(selected.get("host_id") or ""),
            rig_id=selected_rig,
            rig_generation=int(selected.get("generation") or selected.get("rig_generation") or 0),
            installed_build=installed,
            observed_at=installed_at,
            local=selected_rig == str(local_identity.rig_id),
        )

    def _observation(
        self,
        app: str,
        *,
        source: str,
        host_id: str,
        rig_id: str,
        rig_generation: int,
        installed_build: Mapping[str, Any],
        observed_at: str,
        local: bool,
    ) -> dict[str, Any]:
        fingerprint: str | None = None
        if local:
            try:
                fingerprint = str(self.package_store.install_target_fingerprint())
            except Exception:  # noqa: BLE001 - 目标不可解析时给 None，准入侧 fail-closed
                fingerprint = None
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "app": app,
            "source": source,
            "rig": {"host_id": host_id, "rig_id": rig_id, "rig_generation": rig_generation},
            "target_fingerprint": fingerprint,
            "installed_build": dict(installed_build),
            "observed_at": observed_at,
        }

    def install(
        self,
        app: str,
        slot: str | None,
        *,
        build_id: str | None = None,
        verify: bool = True,
        target_rig_id: str | None = None,
        device: str | None = None,
        priority: str = "normal",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """安装一个 Build；控制面已配置时提交 provisioning Assignment。

        控制面已配置时目标 Rig 必须固定（BL-218）：给 ``device`` 时按
        ``resolve_device_rig`` 解析唯一 Rig，显式 ``target_rig_id`` 与解析结果不同则拒绝；
        只给 ``target_rig_id`` 时原样采用；两者都缺时拒绝。拒绝抛
        ``TargetRigPinRefused``（``device_required`` / ``device_rig_unresolved`` /
        ``target_rig_device_mismatch``），发生在解析 Build 与提交 Assignment 之前。
        未配置控制面的兼容路径不变，``device`` 在该路径上不参与判定。
        """
        # 点名了目标 Rig 却没有控制面：先拒绝，连 Build 都不解析。兼容路径只会在本机
        # 装，``target_rig_id`` 在那条路径上无处可用；继续执行等于参数被接受却不起作用，
        # 控制面里也没有任何派单记录。
        control = self.control_provider()
        if control is None and target_rig_id:
            raise ControlPlaneRequiredForRigTarget(
                f"target_rig_id={target_rig_id!r} 要求经控制面派单，但本机没有配置控制面"
                "（AUTOPHONE_DATABASE_URL 未设，且 Rig 运行目录的 control-plane.env 里没有 "
                "DSN）；不代为落回单 VM 兼容路径"
            )
        if control is not None:
            target_rig_id = pin_target_rig(
                control, device, target_rig_id,
                subject="安装任务", device_optional_with_explicit=True,
            )
        # Build Catalog 解析完整 Build Ref：``build_id`` 钉死一个 Build；只给 slot 取该槽
        # Candidate；都不给按 ``default_slot`` 解析。解析结果原样交给 Provisioning /
        # Device Control，中途不再有第二次「slot → Build」的猜测。
        build_ref = self.package_store.resolve_build_ref(app, slot=slot, build_id=build_id)
        ref_dict = dict(build_ref.to_dict()) if hasattr(build_ref, "to_dict") else dict(build_ref)
        if control is not None:
            assignment = control.submit_assignment(
                {
                    "kind": "provisioning",
                    "requires_rig": True,
                    "priority": priority,
                    "target_rig_id": target_rig_id,
                    "required_capabilities": ["vm_uds", "app_install"],
                    "desired_state": {"app": app, "build_ref": ref_dict},
                },
                idempotency_key
                or f"package-install:{app}:{ref_dict.get('build_id')}:{uuid.uuid4().hex}",
            )
            return {
                "app": app,
                "slot": ref_dict.get("slot"),
                "build_id": ref_dict.get("build_id"),
                "assignment_id": assignment["assignment_id"],
                "status": assignment["status"],
            }

        # 单 VM 兼容路径：本类扮演 Provisioning——安装开始即使旧观测失效，只有 Device
        # Control 回执 verified=True 才发布新的观测事实。
        self.local_observed_state.publish_installed_build(None)
        result = self.package_store.install_build_ref(build_ref, verify=verify)
        if result.get("verified") is True:
            self.local_observed_state.publish_installed_build(
                _observed_installed_build(app, result)
            )
        return result


def _observed_installed_build(app: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """The Build fact published to Rig observed state after a verified install.

    ``installed_build`` alone is the slot-level PackageEntry projection: it has
    no ``build_id``, so an observed state built from it cannot name the exact
    Build.  The installer (Device Control) also returns the resolved
    ``build_ref``; the Build identity keys from it win, and the entry projection
    stays for the descriptive fields (label, file, size) that the ref does not
    carry.  ``verified`` / ``installed_at`` / ``installer`` come from the Install
    Receipt so the observation carries the same evidence Provisioning publishes.
    """
    build = dict(result.get("installed_build") or {})
    ref = result.get("build_ref")
    if isinstance(ref, Mapping):
        build.update(dict(ref))
    build.setdefault("app", app)
    build["verified"] = True
    for key in ("installed_at", "installer"):
        if result.get(key):
            build[key] = result[key]
    return build


__all__ = ["PackageInstallApplication", "TargetRigPinRefused"]
