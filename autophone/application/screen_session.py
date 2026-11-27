"""屏幕锁会话的 Application 操作（M8 切片 7，BL-010）。

目标架构 §11.1：HTTP、CLI 与 Dashboard 只做认证、参数校验与结果转换，调用一个
application 操作，**不在 handler 内驱动设备锁**。本模块提供三个操作，取代此前那层为满足
入口边界守卫而存在的薄转发 Seam（M8 切片 7 已删除）：

- `resolve_target`：把 socket 与 ScreenLock 设备键解析成一个 `ScreenTarget`；
- `run_locked`：在设备屏幕锁内执行一次调用方给的操作，锁的获取与释放由本操作持有
  （含异常路径释放）；
- `ax_nodes`：开一次 AX 会话，取回一屏节点。

与被取代的转发层的区别：转发层只把设备协议适配器的名字换个地方 import，锁仍由 CLI /
HTTP handler 自己 `with lock.acquire(...)` 驱动；这里锁的生命周期在操作内部，入口只传参
取结果。设备适配器抛出的绑定错误统一翻成 `ScreenSessionError`，入口不必点名
`autophone.device` 的异常类型（`tests/test_entrypoint_boundaries.py`）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


class ScreenSessionError(RuntimeError):
    """设备目标无法绑定，或屏幕锁不可用。入口把它翻成自己的协议错误。"""


@dataclass(frozen=True)
class ScreenTarget:
    """一次碰屏工作流选中的设备身份。

    字段与 `autophone.device.target.VPhoneTarget` 同名同义，`public_dict()` 的输出逐键
    相同——它已经是 Dashboard 与证据里的既有字段名，改名会让存量回执对不上。
    """

    device: str
    socket: Path
    vm_dir: Path
    cli_dir: Path

    @property
    def sock_path(self) -> str:
        """socket 的 POSIX 字符串形式（Adapter 的 `sock_path=` 参数用这个）。"""
        return self.socket.as_posix()

    @property
    def uses_custom_socket(self) -> bool:
        """选中的 socket 是否偏离该 VM 目录的默认 socket。

        偏离时 AX 会话无法从 VM 目录推出 frida 主机，调用方必须显式给出。
        """
        return self.socket != (self.vm_dir / "vphone.sock").expanduser().resolve()

    def public_dict(self) -> dict[str, str]:
        return {
            "device": self.device,
            "vm": self.vm_dir.name,
            "vm_dir": str(self.vm_dir),
            "socket": str(self.socket),
        }


class ScreenSessionApplication:
    """设备屏幕锁会话：解析目标、在锁内执行一次操作、读一屏 AX 节点。"""

    def resolve_target(
        self,
        *,
        sock: Path | str | None = None,
        device: str | None = None,
    ) -> ScreenTarget:
        """解析 socket 与 ScreenLock 设备键。绑定不成立时抛 `ScreenSessionError`。"""
        from autophone.device.target import (
            VPhoneTargetBindingError,
            resolve_screen_lock_target,
        )

        try:
            bound = resolve_screen_lock_target(sock=sock, device=device)
        except VPhoneTargetBindingError as exc:
            raise ScreenSessionError(str(exc)) from exc
        return ScreenTarget(
            device=bound.device,
            socket=bound.sock,
            vm_dir=bound.vm_dir,
            cli_dir=bound.cli_dir,
        )

    def run_locked(
        self,
        operation: Callable[[], T],
        *,
        device: str,
        run_id: str,
        lock_root: Path | None = None,
    ) -> T:
        """在 `device` 的屏幕锁内执行 `operation()` 并返回它的结果。

        锁在进入前获取、退出时释放，`operation` 抛异常也照样释放（`acquire` 的
        `finally` 语义）——这是本操作相对旧转发层的实质变化：调用方拿不到锁对象，
        也就无从漏掉释放。`lock_root=None` 由 `ScreenLock` 取默认锁根；只有需要与既有
        实验共享一把锁的调用方才显式给根。
        """
        from autophone.device.lock import ScreenLock

        with ScreenLock(root=lock_root).acquire(device=device, run_id=run_id):
            return operation()

    def ax_nodes(
        self,
        *,
        bundle_id: str,
        sock_path: str,
        host: str | None = None,
        ax_backend: str | None = None,
    ) -> list[dict[str, Any]]:
        """开一次 AX 会话取一屏节点，返回 identifier / label / frame 三元组列表。

        会话在本操作内开启与关闭；调用方拿到的是纯数据，不持有会话。
        默认使用 ``DEFAULT_AX_BACKEND``（``guest_axruntime``），调用方可显式传入
        ``ax_backend="frida_inproc"``。
        """
        from autophone.device.ax_session import AxSession

        kwargs: dict[str, Any] = {"host": host, "sock_path": sock_path}
        if ax_backend is not None:
            kwargs["ax_backend"] = ax_backend
        with AxSession(bundle_id, **kwargs) as session:
            dump = session.refresh()
        return [
            {
                "identifier": node.identifier,
                "label": node.label,
                "frame": list(node.frame),
            }
            for node in dump.nodes
        ]


__all__ = [
    "ScreenSessionApplication",
    "ScreenSessionError",
    "ScreenTarget",
]
