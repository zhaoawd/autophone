"""vm_fatal 事件判据：双 Rig 观测（live_p0）与单 Rig 基线窗口（baseline_window）共用。

``host_crash_count = max(vm_fatal_events, rig_down_incidents)`` 中的 vm_fatal_events
由本模块计数。两处执行器必须引用同一个谓词常量与同一个计数函数，否则基线与窗口
不同口径。

版本（``vm_fatal_predicate_version``，比较规则见 ``autophone.control.usb_baseline``）：

- v1（2026-10-10 前）：Virtualization 进程的任意 Error 行（messageType 16）或含
  ``Fatal error`` 的行。2026-10-10 的复核表明 Error 行包含与 VM 存活无关的记录：
  每次 VM 启动的 AddInstanceForFactory / _dirhelper 行、ParavirtualizedGraphics 与
  ANECompiler 行，以及宿主蓝牙音频设备断开同秒的 CoreAudio
  ``HALC_ShellDevice::CreateIOContextDescription`` 行（两台 health 全程 healthy）。
- v2：只计 Fault 行（messageType 17）、含 ``Fatal error`` 的行，以及
  ``autophone.usb_diagnosis`` 已结构化的 ``IOUSBHostCIExceptionType*`` 签名行。
  2026-10-02～10-06 本机 Virtualization 进程的 Fault 行全部是
  ``[IOUSBHost:VZIOUSBHostControllerInterface] Fatal error:
  IOUSBHostCIExceptionTypeCommandTimeout``（docs/vphone-vm-usb-crash.md 的崩溃签名）。

``log show`` 先按谓词过滤；``count_vm_fatal_rows`` 再按同一判据逐行复核，只计带时间戳
的日志行，多行消息的续行不再单独计数。带时间戳但无法解析类型或进程的行按命中计
（宁多计，不漏计）。
"""
from __future__ import annotations

import re

from autophone.control.usb_baseline import (
    LEGACY_VM_FATAL_PREDICATE_VERSION,
    VM_FATAL_PREDICATE_VERSION,
)

VM_PROCESS = "com.apple.Virtualization.VirtualMachine"
FATAL_TEXT = "Fatal error"
USB_EXCEPTION_SIGNATURE = "IOUSBHostCIExceptionType"
MESSAGE_TYPE_ERROR = 16
MESSAGE_TYPE_FAULT = 17

LEGACY_VM_FATAL_PREDICATE = (
    f'process == "{VM_PROCESS}" AND '
    f'(messageType == {MESSAGE_TYPE_ERROR} OR eventMessage CONTAINS "{FATAL_TEXT}")'
)
VM_FATAL_PREDICATE = (
    f'process == "{VM_PROCESS}" AND '
    f'(messageType == {MESSAGE_TYPE_FAULT} OR eventMessage CONTAINS "{FATAL_TEXT}" '
    f'OR eventMessage CONTAINS "{USB_EXCEPTION_SIGNATURE}")'
)
VM_FATAL_PREDICATES = {
    LEGACY_VM_FATAL_PREDICATE_VERSION: LEGACY_VM_FATAL_PREDICATE,
    VM_FATAL_PREDICATE_VERSION: VM_FATAL_PREDICATE,
}

# `log show --style compact` 的类型列：Df=Default, I=Info, Db=Debug, E=Error, F=Fault。
_COMPACT_TYPES = {"Df": 0, "I": 1, "Db": 2, "E": MESSAGE_TYPE_ERROR, "F": MESSAGE_TYPE_FAULT}
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s")
_ROW_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+(?P<type>[A-Za-z]+)\s+"
    r"(?P<process>[^\s\[]+)\[[^\]]*\]\s?(?P<message>.*)$"
)


def _row_matches(line: str, version: int) -> bool:
    match = _ROW_RE.match(line)
    if match is None:
        return True  # 有时间戳但格式未知：按命中计。
    if match.group("process") != VM_PROCESS:
        return False
    message_type = _COMPACT_TYPES.get(match.group("type"))
    message = match.group("message")
    if version == LEGACY_VM_FATAL_PREDICATE_VERSION:
        if message_type is None:
            return True
        return message_type == MESSAGE_TYPE_ERROR or FATAL_TEXT in message
    if message_type is None:
        return True
    return (
        message_type == MESSAGE_TYPE_FAULT
        or FATAL_TEXT in message
        or USB_EXCEPTION_SIGNATURE in message
    )


def count_vm_fatal_rows(text: str, *, version: int = VM_FATAL_PREDICATE_VERSION) -> int:
    """Count unified-log rows that satisfy the vm_fatal predicate of ``version``."""

    if version not in VM_FATAL_PREDICATES:
        raise ValueError(f"unknown vm_fatal predicate version: {version!r}")
    return sum(
        1
        for line in text.splitlines()
        if _TIMESTAMP_RE.match(line) and _row_matches(line, version)
    )


__all__ = [
    "LEGACY_VM_FATAL_PREDICATE",
    "LEGACY_VM_FATAL_PREDICATE_VERSION",
    "VM_FATAL_PREDICATE",
    "VM_FATAL_PREDICATES",
    "VM_FATAL_PREDICATE_VERSION",
    "VM_PROCESS",
    "count_vm_fatal_rows",
]
