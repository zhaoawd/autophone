"""Adapter protocol kept independent of Frida and device implementations."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import AdapterAck, LocationSample


@runtime_checkable
class LocationAdapter(Protocol):
    name: str

    def start(self) -> None: ...

    def deliver(self, sample: LocationSample) -> AdapterAck: ...

    def stop(self) -> None: ...
