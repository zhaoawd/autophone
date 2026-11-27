"""Instrumented App Session with immutable Evidence freeze semantics."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from autophone.device import DeviceSession


@dataclass(frozen=True)
class ObservationIdentity:
    host_id: str
    rig_id: str
    rig_generation: int


class ObservationDevice:
    """Read-only collector capability; no shell, transport or write methods."""

    def __init__(self, device: DeviceSession) -> None:
        self.__device = device
        self._active = False
        target = device.target
        self.target = ObservationIdentity(
            str(getattr(target, "host_id", "")), str(target.rig_id),
            int(getattr(target, "rig_generation", 0)),
        )

    def _check_active(self) -> None:
        if not self._active:
            raise RuntimeError("Observation device capability is not active")

    def screenshot(self) -> bytes:
        self._check_active()
        return self.__device.screenshot()

    def app_list(self) -> list[dict[str, Any]]:
        self._check_active()
        return deepcopy(self.__device.app_list())

    def read_installed_build(self, app: str, bundle_id: str):
        self._check_active()
        return self.__device.read_installed_build(app, bundle_id)


class ObservationSession(Protocol):
    def start(self) -> "ObservationSession": ...
    def mark(self, step_identity: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def stop(self) -> "FrozenEvidence": ...


class EvidenceCollector(Protocol):
    name: str

    def start(self, device: ObservationDevice) -> None: ...
    def stop(self) -> None: ...
    def freeze(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class FrozenEvidence:
    planes: Mapping[str, Mapping[str, Any]]
    collector_order: tuple[str, ...]

    def plane(self, name: str) -> Mapping[str, Any]:
        return self.planes.get(name, MappingProxyType({}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "collector_order": list(self.collector_order),
            "planes": deepcopy({name: dict(value) for name, value in self.planes.items()}),
        }


class InstrumentedAppSession:
    """Own read-only collector lifecycles around one Run execution."""

    def __init__(
        self,
        device: DeviceSession,
        collectors: Sequence[EvidenceCollector] = (),
    ) -> None:
        names = [collector.name for collector in collectors]
        if len(names) != len(set(names)):
            raise ValueError("collector names must be unique")
        self.device = ObservationDevice(device)
        self.collectors = tuple(collectors)
        self._entered = False
        self._sequence = 0
        self._stop_error: RuntimeError | None = None
        self._started: list[EvidenceCollector] = []
        self._frozen: FrozenEvidence | None = None

    def __enter__(self) -> "InstrumentedAppSession":
        if self._entered or self._frozen is not None:
            raise RuntimeError("Observation session cannot be restarted")
        self._entered = True
        self.device._active = True
        try:
            for collector in self.collectors:
                self._started.append(collector)
                collector.start(self.device)
        except Exception:
            self._stop()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._frozen is not None:
            return
        errors = self._stop()
        planes = {}
        for collector in self.collectors:
            try:
                value = deepcopy(dict(collector.freeze()))
            except Exception as freeze_error:  # noqa: BLE001 - preserve other planes
                value = {
                    "status": "freeze_error",
                    "error": f"{type(freeze_error).__name__}: {freeze_error}",
                }
                errors.append(freeze_error)
            planes[collector.name] = MappingProxyType(value)
        self._frozen = FrozenEvidence(
            planes=MappingProxyType(planes),
            collector_order=tuple(collector.name for collector in self.collectors),
        )
        if errors:
            self._stop_error = RuntimeError(
                "; ".join(f"{type(error).__name__}: {error}" for error in errors)
            )
            if exc is None:
                raise self._stop_error

    def start(self) -> "InstrumentedAppSession":
        return self.__enter__()

    def mark(self, step_identity: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.device._active or self._frozen is not None:
            raise RuntimeError("Observation markers require an active session")
        self._sequence += 1
        receipt = {"sequence": self._sequence, "step_identity": deepcopy(dict(step_identity))}
        for collector in self._started:
            marker = getattr(collector, "mark", None)
            if callable(marker):
                marker(deepcopy(receipt))
        return receipt

    def stop(self) -> FrozenEvidence:
        if self._stop_error is not None:
            raise self._stop_error
        if self._frozen is None:
            if not self._entered:
                raise RuntimeError("Observation session has not started")
            self.__exit__(None, None, None)
        return self.freeze()

    def freeze(self) -> FrozenEvidence:
        if self._frozen is None:
            raise RuntimeError("Evidence can be frozen only after the session stops")
        return self._frozen

    def _stop(self) -> list[Exception]:
        errors = []
        for collector in reversed(self._started):
            try:
                collector.stop()
            except Exception as exc:  # noqa: BLE001 - finish remaining collectors
                errors.append(exc)
        self._started.clear()
        self.device._active = False
        return errors
