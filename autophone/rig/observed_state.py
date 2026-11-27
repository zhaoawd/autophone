"""Filesystem Adapter for single-Rig compatibility observed state."""
from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.device.target import resolve_target
from autophone.paths import STATE_ROOT
from autophone.util import now_iso, read_json, write_json_atomic


class RigObservedStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RigObservedIdentity:
    host_id: str
    rig_id: str
    rig_generation: int

    def __post_init__(self) -> None:
        if not self.host_id or not self.rig_id:
            raise RigObservedStateError("host_id and rig_id are required")
        if self.rig_generation <= 0:
            raise RigObservedStateError("rig_generation must be positive")

    @classmethod
    def compatibility(cls, environ: Mapping[str, str] | None = None) -> "RigObservedIdentity":
        env = os.environ if environ is None else environ
        target = resolve_target(environ=env)
        try:
            generation = int(env.get("AUTOPHONE_RIG_GENERATION") or 1)
        except ValueError as exc:
            raise RigObservedStateError("AUTOPHONE_RIG_GENERATION must be an integer") from exc
        return cls(
            host_id=str(env.get("AUTOPHONE_HOST_ID") or platform.node() or "local-host"),
            rig_id=str(env.get("AUTOPHONE_RIG_ID") or target.device),
            rig_generation=generation,
        )


class FileRigObservedState:
    """Persist the same installed_build fact used by PostgreSQL Rig state."""

    def __init__(self, identity: RigObservedIdentity, *, root: Path = STATE_ROOT,
                 active_update_validator: Callable[..., Mapping[str, Any]] | None = None) -> None:
        self.identity = identity
        self._active_update_validator = active_update_validator
        token = hashlib.sha256(
            f"{identity.host_id}\0{identity.rig_id}\0{identity.rig_generation}".encode("utf-8")
        ).hexdigest()[:24]
        self.path = root / "rig-observed-state" / f"{token}.json"

    @classmethod
    def compatibility(
        cls,
        *,
        root: Path = STATE_ROOT,
        environ: Mapping[str, str] | None = None,
        active_update_validator: Callable[..., Mapping[str, Any]] | None = None,
    ) -> "FileRigObservedState":
        return cls(RigObservedIdentity.compatibility(environ), root=root,
                   active_update_validator=active_update_validator)

    def snapshot(self) -> dict[str, Any]:
        try:
            raw = read_json(self.path)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise RigObservedStateError(
                f"Cannot read Rig observed state: {self.path}"
            ) from exc
        if raw is None:
            return self._document(None)
        if not isinstance(raw, Mapping):
            raise RigObservedStateError(
                f"Rig observed-state document must be an object: {self.path}"
            )
        expected = {
            "host_id": self.identity.host_id,
            "rig_id": self.identity.rig_id,
            "rig_generation": self.identity.rig_generation,
        }
        if any(raw.get(key) != value for key, value in expected.items()):
            raise RigObservedStateError(f"Rig observed-state identity mismatch: {self.path}")
        observed = raw.get("observed_state")
        if not isinstance(observed, Mapping):
            raise RigObservedStateError(f"Rig observed_state must be an object: {self.path}")
        return dict(raw)

    def installed_build(self, app: str) -> dict[str, Any] | None:
        candidate = self.snapshot()["observed_state"].get("installed_build")
        if not isinstance(candidate, Mapping) or candidate.get("app") != app:
            return None
        return dict(candidate)

    def publish_installed_build(
        self,
        build: Mapping[str, Any] | None,
        *,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """发布本机已安装 Build 观测。

        `updated_at` 省略时取当前时间（生产路径）。传入固定时间是给测试用的时钟注入点：
        观测文档的 `observed_at` 直接取自这里，冻结进轮次目录后要与现场重算的文档逐字节
        相等，真实时钟跨秒会让两次取值不同。
        """
        document = self.snapshot()
        observed = dict(document["observed_state"])
        observed["installed_build"] = dict(build) if build is not None else None
        # A new installation invalidates the previous runtime update observation.
        observed["active_update"] = None
        document["observed_state"] = observed
        document["updated_at"] = updated_at or now_iso()
        write_json_atomic(self.path, document)
        return document

    def publish_active_update(
        self,
        receipt: Mapping[str, Any] | None,
        *,
        target_fingerprint: str,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist a recomputed compatibility observation, or clear active_update.

        This filesystem adapter does not grant Assignment or device authority.
        Managed Rig execution must use the fenced Control Plane publisher.
        """
        document = self.snapshot()
        if receipt is None:
            observed = dict(document["observed_state"])
            observed["active_update"] = None
        else:
            if self._active_update_validator is None:
                raise RigObservedStateError("active_update validator is required")
            observed = dict(self._active_update_validator(
                receipt, observed_state=document["observed_state"],
                target_fingerprint=target_fingerprint,
            ))
        document["observed_state"] = observed
        document["updated_at"] = updated_at or now_iso()
        write_json_atomic(self.path, document)
        return document

    def _document(
        self,
        installed_build: Mapping[str, Any] | None,
        *,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "v1",
            "host_id": self.identity.host_id,
            "rig_id": self.identity.rig_id,
            "rig_generation": self.identity.rig_generation,
            "observed_state": {
                "installed_build": (
                    dict(installed_build) if installed_build is not None else None
                ),
            },
            "capability_state": {},
            "updated_at": updated_at or now_iso(),
        }


__all__ = [
    "FileRigObservedState",
    "RigObservedIdentity",
    "RigObservedStateError",
]
