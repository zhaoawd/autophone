"""Frida 脚本导出面的版本差异垫片。

frida >= 16 用同步的 ``script.exports_sync``，更早的版本用 ``script.exports``。
这一个判断被设备层（``autophone.device.frida``）、观测层与定位层共用，放在设备层
是因为它描述的是本机 frida-python 的形态，不是任何一种观测证据（BL-003 判据 A：
``device`` 不得反向 import ``observation``）。
"""

from __future__ import annotations

from typing import Any


def script_exports(script: Any) -> Any:
    """Return the script's RPC export surface across frida-python versions."""
    return getattr(script, "exports_sync", None) or script.exports
