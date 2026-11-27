"""vphone_mcp: ScreenLock-aware MCP server and UDS client for vphone-cli iOS VMs."""
from __future__ import annotations

from .client import (
    CommandNotAdmitted,
    GeometryDrift,
    NotReady,
    PROTOCOL_LEGACY,
    PROTOCOL_UPSTREAM_V1,
    ProtocolError,
    VPhoneClient,
)
from .coordination import (
    ControlPlaneUnavailable,
    CoordinationConfig,
    CoordinationConfigError,
    CoordinationError,
    DeviceCoordinator,
    LocalScreenLock,
    LockRequiredError,
    MODE_CONTROL_PLANE,
    MODE_LOCAL_LOCK,
    MODE_MANAGED_PROXY,
    MODE_STANDALONE,
    ScreenLockBusy,
    ScreenQuarantineMarkerPresent,
    resolve_coordination_config,
)

__all__ = [
    "CommandNotAdmitted",
    "ControlPlaneUnavailable",
    "CoordinationConfig",
    "CoordinationConfigError",
    "CoordinationError",
    "DeviceCoordinator",
    "GeometryDrift",
    "LocalScreenLock",
    "LockRequiredError",
    "MODE_CONTROL_PLANE",
    "MODE_LOCAL_LOCK",
    "MODE_MANAGED_PROXY",
    "MODE_STANDALONE",
    "NotReady",
    "PROTOCOL_LEGACY",
    "PROTOCOL_UPSTREAM_V1",
    "ProtocolError",
    "ScreenLockBusy",
    "ScreenQuarantineMarkerPresent",
    "VPhoneClient",
    "resolve_coordination_config",
]
