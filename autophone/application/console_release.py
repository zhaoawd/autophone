"""操作会话释放时的设备复位（BL-104，设计文档 §7.3）。

契约：**回主屏，保留登录**。它是 `DeviceConsoleManager` 的 `release_cleanup` 钩子，
在同一条清理流程里、**还锁之前**执行（§8.3 的顺序：进 closing → 等在飞动作落地 →
复位 → 释放预留 → 还锁）。抛异常即「清理失败」，走 §8.4：记 `error_reason`、设备
进入不可分配状态、**不还锁**。所以本模块的每一条不确定路径都必须抛，不能静默跳过。

复位动作取值（一期实现注）：

- 默认策略 `ui_home`，执行为**按一次 iOS home 键**（UDS `key`）。它把画面带回主屏、
  不 kill app，因此登录态保留。
- 与用例复位链路里的 `ui_home` 不是同一个动作：那边指「app 内回首页」，靠 recipe
  一步步退；这里只保证「离开上一位用户停在的页面、回到主屏」，不进 app 内导航。
  两者同名但作用域不同，取值差异记在设计文档 §6.6。
- 会话不带 app（一期的 grant 没有 app 字段），所以恒走默认策略；`app_resolver`
  返回了 app 时改读该 app profile 的 `reset`：
  `ui_home` → 按 home 键；`none` → 不动；其余取值（如 `relaunch`）与「查不到 profile」
  一律抛错 fail-closed——`relaunch` 要杀进程重启，不在「保留登录且不改状态」的承诺内。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 设备当前 app 未知时的复位策略（§7.3 的默认策略）。
DEFAULT_RESET_POLICY = "ui_home"
# 本模块认识的复位策略。profile 里的 `relaunch` 不在其中，见模块 docstring。
SUPPORTED_RESET_POLICIES = ("ui_home", "none")

HOME_KEY = "home"


class ReleaseResetError(RuntimeError):
    """复位没能确认完成。抛给 `_close`，由它记 `error_reason` 并让设备进不可分配。"""


class ConsoleReleaseReset:
    """`release_cleanup(device, session_id)` 的实现。

    注入三件：

    - `driver_factory`：造一个带 `press_key(name)` 的设备驱动。会话此刻仍持 lease，
      所以新开一条 UDS 连接是安全的；
    - `app_resolver(device, session_id) -> str | None`：这条会话对应的 app。一期恒
      返回 `None`（grant 不记 app），留作 BL-105 会话内场景落地后按 app 复位的接口；
    - `profile_loader(app) -> Mapping`：读 app profile。查不到就抛，由本类转成
      `ReleaseResetError`。
    """

    def __init__(
        self,
        *,
        driver_factory: Callable[[], Any],
        app_resolver: Callable[[str, str], str | None] | None = None,
        profile_loader: Callable[[str], Any] | None = None,
    ) -> None:
        self._driver_factory = driver_factory
        self._app_resolver = app_resolver
        self._profile_loader = profile_loader

    def __call__(self, device: str, session_id: str) -> None:
        policy = self._policy(device, session_id)
        if policy == "none":
            logger.info(
                f"device console release reset skipped: device={device} session={session_id} "
                "policy=none"
            )
            return
        try:
            driver = self._driver_factory()
            driver.press_key(HOME_KEY)
        except Exception as exc:  # noqa: BLE001 - 复位没做成不能宣告设备可复用
            raise ReleaseResetError(
                f"could not return {device} to the home screen: {exc}"
            ) from exc
        logger.info(
            f"device console release reset: device={device} session={session_id} "
            f"policy={policy}（回主屏，保留登录）"
        )

    def _policy(self, device: str, session_id: str) -> str:
        app = None
        if self._app_resolver is not None:
            app = self._app_resolver(device, session_id)
        app = str(app or "").strip()
        if not app:
            # 设备当前 app 未知：走 §7.3 的默认策略。它不依赖 app，所以这不是
            # 「不确定就放过」——按 home 键在任何前台 app 下都把设备带回主屏。
            return DEFAULT_RESET_POLICY
        if self._profile_loader is None:
            raise ReleaseResetError(
                f"session {session_id} is bound to app {app} but no profile loader is configured"
            )
        try:
            profile = self._profile_loader(app)
        except Exception as exc:  # noqa: BLE001 - 无 profile 即无法确认复位策略
            raise ReleaseResetError(f"could not load the profile of app {app}: {exc}") from exc
        policy = str((profile or {}).get("reset") or "").strip()
        if policy not in SUPPORTED_RESET_POLICIES:
            raise ReleaseResetError(
                f"app {app} declares reset={policy!r}, which this release path does not "
                f"implement (supported: {', '.join(SUPPORTED_RESET_POLICIES)})"
            )
        return policy


__all__ = [
    "DEFAULT_RESET_POLICY",
    "SUPPORTED_RESET_POLICIES",
    "ConsoleReleaseReset",
    "ReleaseResetError",
]
