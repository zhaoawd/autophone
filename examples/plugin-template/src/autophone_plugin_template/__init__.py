"""Minimal third-party entry-point provider template for Autophone Public Core."""
from __future__ import annotations

from .admission import validate_run_admission
from .device_session import TemplateDeviceSessionOps, create_device_session_ops
from .executor import TemplateAssignmentExecutor, create_executor

__all__ = [
    "TemplateAssignmentExecutor",
    "TemplateDeviceSessionOps",
    "create_device_session_ops",
    "create_executor",
    "validate_run_admission",
]
