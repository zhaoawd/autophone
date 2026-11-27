"""Rig Agent, explicit DeviceTarget, and device-write fencing."""

from .agent import (
    AgentExecutionContext,
    AssignmentCancelled,
    AssignmentControl,
    DeviceWriteFence,
    ExecutorContractError,
    RigAgent,
    StaleDeviceWrite,
)
from .target import DeviceTarget, TargetConfigurationError

_LIFECYCLE_EXPORTS = {
    "LifecycleReceipt",
    "RigLifecycle",
    "RigLifecycleError",
}


def __getattr__(name: str):
    if name in _LIFECYCLE_EXPORTS:
        from . import lifecycle

        value = getattr(lifecycle, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AgentExecutionContext",
    "AssignmentCancelled",
    "AssignmentControl",
    "DeviceTarget",
    "DeviceWriteFence",
    "ExecutorContractError",
    "LifecycleReceipt",
    "RigAgent",
    "RigLifecycle",
    "RigLifecycleError",
    "StaleDeviceWrite",
    "TargetConfigurationError",
]
