"""Converge Desired Build and publish generation-bound observed state.

目标架构 §5.4 / §16 P4（BL-011）的三段分工在这里落成一条线：

- Build Catalog 已经把 Desired State 里的 ``build_ref`` 解析成**完整 Build Ref**
  （app / slot / build_id / artifact_sha256 …）；本模块不做任何 slot → Build 的解析，
  也不接受只给 slot 的旧形态。
- Provisioning（本模块）做 reconcile + readback：比较设备回读与 Desired，只在必要时执行
  安装；安装后用独立探针重新回读，回读通过才把带 ``build_id`` 的 ``installed_build``
  发布为 ``RigObservedState``。Stage Result 由 Assignment 执行链封存
  （``ProvisioningResult.to_dict()``），含 Device Control 返回的 Install Receipt 作审计。
- Device Control 只执行精确安装动作并返回 Install Receipt；Receipt 不发布权威状态。

构建标记（BL-237）：版本号不能区分同版本号的不同构建（靶 A 所有构建均为 1.0.0 (1)）。
目标 Build 的 IPA 含构建标记（``autophone-ui-eval-build-marker.json``）时，跳过安装还须
设备回读的标记字节与 IPA 内标记字节相同；安装后同样回读核对。目标 IPA 不含标记时沿用
既有判据（已验证观测 + 版本回读）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from autophone.device import DeviceProtocolError, DeviceSession, InstalledBuild
from autophone.logging import get_logger
from autophone.util import now_iso

_LOG = get_logger(__name__)

BUILD_REF_REQUIRED_KEYS = ("app", "slot", "build_id", "artifact_sha256")


class ProvisioningError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProvisioningResult:
    status: str
    changed: bool
    installed_build: Mapping[str, Any] | None
    detail: str
    build_ref: Mapping[str, Any] | None = None
    install_receipt: Mapping[str, Any] | None = None
    readback: Mapping[str, Any] | None = None
    build_marker: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "changed": self.changed,
            "installed_build": dict(self.installed_build) if self.installed_build else None,
            "detail": self.detail,
            "build_ref": dict(self.build_ref) if self.build_ref else None,
            # Device Control 的 Install Receipt：审计/诊断用，不是权威安装状态。
            "install_receipt": dict(self.install_receipt) if self.install_receipt else None,
            "readback": dict(self.readback) if self.readback else None,
            # 构建标记核对（BL-237）：目标 IPA 是否含标记、期望与回读的 sha256。
            "build_marker": dict(self.build_marker) if self.build_marker else None,
        }


ObservedPublisher = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]
# 返回目标 Build 的 IPA 内构建标记字节；IPA 不含标记时返回 None；无法确定时抛异常。
BuildMarkerSource = Callable[[Mapping[str, Any]], "bytes | None"]


def _sha256(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def normalize_desired_build_ref(value: Any) -> dict[str, Any] | None:
    """Desired State 的 ``build_ref``：None/空 → 无期望；否则必须是完整 Build Ref。

    只给 slot 字符串的旧形态 fail-closed：Provisioning 不拥有 Build Catalog，不能替
    调用方决定 slot 里装哪个 Build。
    """
    if value in (None, "", {}):
        return None
    if not isinstance(value, Mapping):
        raise ProvisioningError(
            "desired_state.build_ref must be a complete Build Ref object "
            f"(app/slot/build_id/artifact_sha256), got {type(value).__name__}: {value!r}"
        )
    missing = [key for key in BUILD_REF_REQUIRED_KEYS if not str(value.get(key) or "").strip()]
    if missing:
        raise ProvisioningError(
            "desired_state.build_ref is not a complete Build Ref; missing: " + ", ".join(missing)
        )
    return dict(value)


def _readback_matches(current: InstalledBuild | None, ref: Mapping[str, Any]) -> bool:
    """检查 Desired 的版本字段；调用方还须核验已有精确安装事实。

    版本匹配不能证明 Build Record 身份。此函数只排除版本漂移；只有既有已验证事实
    的 app/slot/build_id/artifact_sha256 同时匹配，且目标 IPA 含构建标记时设备回读的
    标记字节也相同（BL-237），调用方才能跳过安装。
    """
    if current is None:
        return False
    want_short = str(ref.get("short_version") or "")
    want_build = str(ref.get("build_version") or "")
    if not want_short or not want_build:
        return False
    return (
        str(current.short_version or "") == want_short
        and str(current.build_version or "") == want_build
    )


def _verified_observation(
    current: InstalledBuild | None,
    observed_state: Mapping[str, Any],
    *,
    app: str,
    bundle_id: str,
) -> dict[str, Any] | None:
    """核验同一 Rig Generation 的既有安装事实，不从版本号推导 Build 身份。"""
    fact = observed_state.get("installed_build")
    if current is None or not isinstance(fact, Mapping) or fact.get("verified") is not True:
        return None
    if (
        fact.get("app") != app or fact.get("bundle_id") != bundle_id
        or current.app != app or current.bundle_id != bundle_id
        or not fact.get("slot") or not fact.get("installed_at")
        or re.fullmatch(r"bld-[0-9a-f]{12,64}", str(fact.get("build_id") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(fact.get("artifact_sha256") or "")) is None
        or not fact.get("short_version")
        or fact.get("short_version") != current.short_version
        or fact.get("build_version") != current.build_version
    ):
        return None
    return dict(fact)


def _observed_from(ref: Mapping[str, Any], readback: InstalledBuild) -> InstalledBuild:
    return InstalledBuild(
        app=readback.app,
        bundle_id=readback.bundle_id,
        short_version=readback.short_version,
        build_version=readback.build_version,
        slot=str(ref.get("slot") or "") or None,
        sha256=str(ref.get("artifact_sha256") or "") or None,
        build_id=str(ref.get("build_id") or "") or None,
    )


class RigProvisioner:
    def __init__(
        self,
        session: DeviceSession,
        publish_observed: ObservedPublisher,
        *,
        build_marker_source: BuildMarkerSource | None = None,
    ) -> None:
        self.session = session
        self.publish_observed = publish_observed
        self._build_marker_source = build_marker_source or self._stored_build_marker

    def _stored_build_marker(self, build_ref: Mapping[str, Any]) -> bytes | None:
        # 包仓库实现由装配根注入设备会话（与 install_build 同一包仓库）；会话没有该
        # 能力时抛异常，按「不可核对」处理，不跳过安装。
        return self.session.stored_build_marker(build_ref)

    def _expected_marker(self, build_ref: Mapping[str, Any]) -> dict[str, Any]:
        """目标 Build 的构建标记：``present``（含字节）、``absent`` 或 ``unverifiable``。"""
        try:
            raw = self._build_marker_source(build_ref)
        except Exception as exc:  # 包仓库读取失败：不能证明，按不可核对处理
            return {"state": "unverifiable", "reason": f"{type(exc).__name__}: {exc}"}
        if raw is None:
            return {"state": "absent"}
        return {"state": "present", "raw": bytes(raw), "expected_sha256": _sha256(raw)}

    def _device_marker(self, bundle_id: str) -> tuple[bytes | None, str | None]:
        try:
            return bytes(self.session.read_build_marker(bundle_id)), None
        except DeviceProtocolError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _marker_audit(expected: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        audit = {key: value for key, value in expected.items() if key != "raw"}
        audit.update(extra)
        return audit

    def ensure(
        self,
        desired_state: Mapping[str, Any],
        *,
        app: str,
        bundle_id: str,
        capability_state: Mapping[str, Any],
        observed_state: Mapping[str, Any] | None = None,
    ) -> ProvisioningResult:
        build_ref = normalize_desired_build_ref(desired_state.get("build_ref"))
        current = self.session.read_installed_build(app, bundle_id)
        previous = _verified_observation(
            current, observed_state or {}, app=app, bundle_id=bundle_id,
        )
        if build_ref is None:
            if previous is not None:
                self.publish_observed({"installed_build": previous}, capability_state)
            else:
                self._publish(current, capability_state, extra=None)
            return ProvisioningResult(
                status="passed",
                changed=False,
                installed_build=previous or (current.to_dict() if current else None),
                detail="no desired build",
            )
        if str(build_ref.get("app") or "") != app:
            raise ProvisioningError(
                f"desired Build Ref belongs to app {build_ref.get('app')!r}, assignment app is {app!r}"
            )
        expected_marker: dict[str, Any] | None = None
        skip_marker_audit: dict[str, Any] | None = None
        skip = previous is not None and all(
            previous.get(key) == build_ref.get(key) for key in BUILD_REF_REQUIRED_KEYS
        ) and _readback_matches(current, build_ref)
        if skip:
            # 观测与版本号一致不能区分同版本号的不同构建（BL-237）：目标有构建标记时，
            # 设备回读的标记字节也必须相同，否则照常安装。
            expected_marker = self._expected_marker(build_ref)
            if expected_marker["state"] == "present":
                actual, error = self._device_marker(bundle_id)
                skip_marker_audit = self._marker_audit(
                    expected_marker, readback_sha256=_sha256(actual), readback_error=error,
                )
                skip = actual == expected_marker["raw"]
            elif expected_marker["state"] == "unverifiable":
                skip_marker_audit = self._marker_audit(expected_marker)
                skip = False
            else:
                skip_marker_audit = self._marker_audit(expected_marker)
            if not skip:
                _LOG.info(
                    "provisioning reinstalls %s: installed build marker not verified (%s)",
                    build_ref.get("build_id"), skip_marker_audit,
                )
        if skip:
            assert current is not None
            if build_ref.get("source_revision") is not None:
                previous = {**previous, "source_revision": build_ref["source_revision"]}
            self.publish_observed({"installed_build": previous}, capability_state)
            return ProvisioningResult(
                status="passed",
                changed=False,
                installed_build=previous,
                detail="desired build already observed",
                build_ref=build_ref,
                readback=current.to_dict(),
                build_marker=skip_marker_audit,
            )

        # 旧观测在安装开始时就不可信：先发布 None，安装失败时不会留下过期的「已安装」事实。
        self._publish(None, capability_state, extra=None)
        receipt = dict(self.session.install_build(build_ref))
        if receipt.get("verified") is not True:
            raise ProvisioningError(
                "Device Control did not verify the install "
                f"(verified={receipt.get('verified')!r}); no observed state published"
            )
        receipt_ref = dict(receipt.get("build_ref") or {})
        if receipt_ref and any(
            receipt_ref.get(key) != build_ref.get(key)
            for key in (*BUILD_REF_REQUIRED_KEYS, "short_version", "build_version", "source_revision")
        ):
            raise ProvisioningError(
                "Install Receipt names a different Build than the Desired State: "
                f"{receipt_ref.get('build_id')!r} != {build_ref['build_id']!r}"
            )
        if receipt.get("installed_bundle_id") not in (None, bundle_id):
            raise ProvisioningError("Install Receipt names a different bundle_id")
        readback = self.session.read_installed_build(app, bundle_id)
        if readback is None:
            raise ProvisioningError(
                f"installed Build readback missing: app={app} bundle_id={bundle_id}"
            )
        if readback.app != app or readback.bundle_id != bundle_id:
            raise ProvisioningError("installed Build readback identity mismatch")
        expected_version = str(build_ref.get("short_version") or "")
        if expected_version and str(readback.short_version or "") != expected_version:
            raise ProvisioningError(
                f"installed Build readback mismatch: {readback.short_version!r} != {expected_version!r}"
            )
        expected_build_version = str(build_ref.get("build_version") or "")
        if (
            expected_build_version
            and readback.build_version
            and str(readback.build_version) != expected_build_version
        ):
            raise ProvisioningError(
                "installed Build readback build_version mismatch: "
                f"{readback.build_version!r} != {expected_build_version!r}"
            )
        if expected_marker is None:
            expected_marker = self._expected_marker(build_ref)
        marker_audit = self._marker_audit(expected_marker)
        if expected_marker["state"] == "present":
            actual, error = self._device_marker(bundle_id)
            marker_audit.update(readback_sha256=_sha256(actual), readback_error=error)
            if actual != expected_marker["raw"]:
                raise ProvisioningError(
                    "installed Build readback build marker mismatch: "
                    f"{_sha256(actual) or error!r} != {expected_marker['expected_sha256']!r}"
                )
        observed = _observed_from(build_ref, readback)
        self._publish(
            observed,
            capability_state,
            extra={
                "verified": True,
                "installed_at": str(receipt.get("installed_at") or "") or now_iso(),
                "installer": receipt.get("installer"),
                "source_revision": build_ref.get("source_revision"),
            },
        )
        return ProvisioningResult(
            status="passed",
            changed=True,
            installed_build=observed.to_dict(),
            detail="desired build installed and read back",
            build_ref=build_ref,
            install_receipt=receipt,
            readback=readback.to_dict(),
            build_marker=marker_audit,
        )

    def _publish(
        self,
        installed: InstalledBuild | None,
        capability_state: Mapping[str, Any],
        *,
        extra: Mapping[str, Any] | None,
    ) -> None:
        fact: dict[str, Any] | None = None
        if installed is not None:
            fact = installed.to_dict()
            # RigObservedState.installed_build 用 Build Ref 的字段名（artifact_sha256），
            # 与 PackageInstallApplication 兼容路径发布的事实同形，准入侧只认一种形状。
            fact["artifact_sha256"] = fact.pop("sha256", None)
            if extra:
                fact.update({k: v for k, v in extra.items() if v is not None})
        observed_state = {"installed_build": fact}
        self.publish_observed(observed_state, capability_state)
