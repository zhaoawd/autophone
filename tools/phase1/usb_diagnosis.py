"""Parse typed USB exception and usbmux connection evidence."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re


LOCKDOWN_PORT = 62078
EXCEPTION_RE = re.compile(r"IOUSBHostCIExceptionType(?P<kind>[A-Za-z0-9_]+)")
TIMESTAMP_RE = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")
USBMUX_CONNECT_RE = re.compile(
    r"USBMuxConnectByPort.*?Connecting to port (?P<port>\d+)"
    r"(?: \((?P<low>[0-9a-fA-F]{2}), (?P<high>[0-9a-fA-F]{2})\))?"
)


@dataclass(frozen=True)
class UsbException:
    timestamp: str | None
    kind: str
    line: str


@dataclass(frozen=True)
class UsbmuxConnect:
    timestamp: str | None
    raw_port: int
    target_port: int
    is_lockdownd: bool
    line: str


@dataclass(frozen=True)
class UsbIncidentDiagnosis:
    exception_counts: dict[str, int]
    exceptions: list[UsbException]
    first_exception: UsbException | None
    usbmux_connects: list[UsbmuxConnect]
    lockdownd_connect_count: int


def byte_swap_u16(value: int) -> int:
    """Return ``value`` after swapping its two bytes."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"not a uint16: {value}")
    return ((value & 0xFF) << 8) | (value >> 8)


def _timestamp(line: str) -> str | None:
    match = TIMESTAMP_RE.match(line)
    return match.group("timestamp") if match else None


def _exception_kind(line: str) -> str | None:
    match = EXCEPTION_RE.search(line)
    return match.group("kind") if match else None


def diagnose_usb_incident(*, vm_fatal_text: str, usbmux_text: str) -> UsbIncidentDiagnosis:
    """Extract typed VZ USB failures and actual usbmux ConnectByPort attempts."""

    exceptions: list[UsbException] = []
    for line in vm_fatal_text.splitlines():
        kind = _exception_kind(line)
        if kind:
            exceptions.append(UsbException(_timestamp(line), kind, line))

    connects: list[UsbmuxConnect] = []
    for line in usbmux_text.splitlines():
        match = USBMUX_CONNECT_RE.search(line)
        if not match:
            continue
        raw_port = int(match.group("port"))
        target_port = byte_swap_u16(raw_port)
        connects.append(
            UsbmuxConnect(
                timestamp=_timestamp(line),
                raw_port=raw_port,
                target_port=target_port,
                is_lockdownd=target_port == LOCKDOWN_PORT,
                line=line,
            )
        )

    counts = dict(Counter(event.kind for event in exceptions))
    return UsbIncidentDiagnosis(
        exception_counts=counts,
        exceptions=exceptions,
        first_exception=exceptions[0] if exceptions else None,
        usbmux_connects=connects,
        lockdownd_connect_count=sum(event.is_lockdownd for event in connects),
    )
