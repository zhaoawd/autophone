"""前台保活看门狗（dashboard 左上角切 app 后让被选 app 常驻前台）。

语义：dashboard 选中某个 app → `pin()` 记下目标 bundle_id，后台线程每
`interval_s` 拍一次——查 UDS `app_foreground`；若不是被钉的目标就把它拉回来（被切走/
退后台/崩溃冷启都覆盖）。UDS 前台来源不可信时直接进入降级模式：BL-232 阶段 1（P1）
起不再以 Frida attach SpringBoard 读 visibility 作后备——attach 并 detach SpringBoard
后会留下 Frida agent 映射残留（backlog §9.69），之后的安装会被安装前残留检查拒绝。

与回归运行（占用设备）冲突时**暂停保活**：每拍先看 ScreenLock，被 run 锁住就
跳过这拍（不抢设备），run 结束自动恢复。全程 fail-soft：设备不可达 / app_foreground
报错只记 last 状态、不抛、不死循环 uiopen。

降级模式（guest 无可信 app_foreground）不能把「进程仍在跑」误判为「仍在前台」：
进程可能已被用户切到后台。为避免每拍触发 app 的 deactivate/activate，降级拍以
``degraded_cooldown_s`` 限频，执行一次 uiopen。
由于前台读数不可用，这次恢复只能标记为「未验证」，绝不伪报成功。

图标兜底（Home 键 → 点主屏图标）自带确定性副作用：目标若已在前台，Home 会把它
按回桌面再拉起，keeper 反成闪动源（2026-07-10 视频实锤）。所以降级路径（含 strong）
只在**确认进程不存在**（冷启，无前台操作可打断）时才走图标兜底；「进程存活 +
前台不可读」只发 uiopen（对前台 app 是 SpringBoard no-op）。代价：这台 VM 上
uiopen 偶见「返回 0 却不实际前台化」，此时后台存活的 app 拉不回来——有意取舍
（宁可保活失效，不可打断前台），根治等 guest 提供 RBS/FBS frontmost 能力。

精准模式也不可尽信：iOS 26 上旧 guest 的 SBS 前台查询会返回陈旧应用（2026-07-10
活 VM 实测：被测 App 已在前台仍报 Settings）；新 guest 则会诚实返回 ``source=unknown``。
照信旧读数就会每拍 uiopen。
所以精准拍 uiopen 后**回读校验**：读数连续不翻转到目标、而目标明明在跑 → 判定前台
信号不可信，自动降级到上面的安全逻辑。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

_KEEPER_MODES = frozenset({"strong", "gentle"})

# 「从未发生」的冷却哨兵。不能用 0.0：冷却判据是 `time.monotonic() - last < cooldown`，
# 而 monotonic() 的参照点由平台决定（刚开机的机器上可能只有几十秒），用 0.0 会让首拍在
# 低 uptime 机器上误判成「仍在冷却」而跳过第一次拉回。-inf 让首拍恒不在冷却，与平台无关。
_NEVER = float("-inf")


class _ForegroundUnsupported(Exception):
    """UDS 无法取得可信前台状态——触发受限恢复（不再有 SpringBoard/Frida 后备）。"""


class ForegroundKeeper:
    """单实例前台保活。线程惰性启动（首次 pin 才起），pin 目标可热切换。"""

    def __init__(
        self,
        *,
        interval_s: float = 5.0,
        enabled: bool = True,
        degraded_cooldown_s: float = 60.0,
        lock_status_fn: Callable[[str], dict] | None = None,
        sock_resolver: Callable[[], str] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self.enabled = enabled
        self.degraded_cooldown_s = degraded_cooldown_s
        self._lock_status_fn = lock_status_fn
        self._sock_resolver = sock_resolver
        self._mu = threading.Lock()
        self._target: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last: dict = {}
        # 三态：None=未探明；True=支持精准前台读；False=guest 无可信前台读。
        self._fg_supported: bool | None = None
        self._degraded_last_uiopen: float = _NEVER
        self._degraded_last_probe: float = _NEVER
        # 精准模式 uiopen 后回读仍非目标的连续次数；达阈值判前台信号不可信→降级。
        self._fg_mismatch_streak: int = 0

    # ---- 控制面 --------------------------------------------------------
    def pin(
        self,
        *,
        app: str,
        bundle_id: str,
        device: str,
        springboard_icon_preview: tuple[int, int] | None = None,
        mode: str = "strong",
    ) -> dict:
        """钉住一个 app 为前台保活目标，确保看门狗线程在跑。

        保活被环境变量关掉时（``enabled=False``）记下目标但不起线程、不抢设备，
        status 里 ``running=False`` 可见，便于排障。
        """
        if mode not in _KEEPER_MODES:
            raise ValueError(f"invalid foreground mode: {mode!r}")
        with self._mu:
            self._target = {
                "app": app,
                "bundle_id": bundle_id,
                "device": device,
                "springboard_icon_preview": springboard_icon_preview,
                "mode": mode,
            }
            # 换（或重新选择）目标时，不让上个 app 的降级冷却延迟首次拉回；并立即
            # 重试 guest 的前台能力，支持 guest 更新后无需重启 dashboard 服务即恢复精准模式。
            self._degraded_last_uiopen = _NEVER
            self._degraded_last_probe = _NEVER
            if self.enabled:
                self._ensure_thread_locked()
        logger.info("前台保活%s mode=%s app=%s bundle=%s device=%s",
                    "已钉住" if self.enabled else "已记录(已禁用,不保活)",
                    mode, app, bundle_id, device)
        return self.status()

    def unpin(self) -> dict:
        """清除保活目标（线程留着空转，下次 pin 复用）。"""
        with self._mu:
            self._target = None
        logger.info("前台保活已解除")
        return self.status()

    def status(self) -> dict:
        with self._mu:
            target = dict(self._target) if self._target else None
            last = dict(self._last)
            running = bool(self._thread and self._thread.is_alive())
        mode = {None: "unknown", True: "precise", False: "degraded"}[self._fg_supported]
        return {
            "pinned": target,
            "enabled": self.enabled,
            "interval_s": self.interval_s,
            "running": running,
            "mode": mode,
            "selected_mode": target.get("mode") if target else None,
            "last": last,
        }

    # ---- 内部 ----------------------------------------------------------
    def _ensure_thread_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="fg-keeper", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 看门狗绝不能因单拍异常退出
                logger.debug("前台保活 tick 异常: %s", exc)
            self._stop.wait(self.interval_s)

    def _note(self, action: str, detail: str) -> None:
        with self._mu:
            self._last = {"action": action, "detail": detail, "at": time.time()}

    def _sock(self) -> str:
        if self._sock_resolver:
            return self._sock_resolver()
        from autophone.execution.engine.actions import resolve_sock
        return resolve_sock()

    def _device_busy(self, device: str) -> bool:
        if not self._lock_status_fn:
            return False
        try:
            return bool(self._lock_status_fn(device).get("locked"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("读 ScreenLock 状态失败，按未占用处理: %s", exc)
            return False

    def _foreground_bundle(self, sock: str) -> str:
        """UDS app_foreground → 当前前台 bundle_id（主屏/无前台返回 ""）。

        UDS 前台源不可信时抛 `_ForegroundUnsupported`（触发受限恢复）；BL-232 阶段 1
        起不再 attach SpringBoard 作后备。与临时「设备不可达」区分：
        - guest 二进制不认识该命令（老 daemon）；
        - 新 daemon 返回 `source="unknown"`——SBS 前台查询在 iOS 26 失效、guest
          诚实报「测不出」（不再瞎猜最大 pid 误报 Spotlight）；
        - 旧 daemon 没有 source 字段，可能稳定返回陈旧 bundle id。
        """
        from autophone.device.uds import request as uds_request

        resp, _ = uds_request({"t": "app_foreground"}, sock_path=sock, timeout=8)
        if not resp.get("ok"):
            err = str(resp.get("error") or "")
            if "unknown" in err.lower():
                raise _ForegroundUnsupported(err)
            raise RuntimeError(f"app_foreground failed: {err!r}")
        source = resp.get("source")
        if source is None:
            # 旧 guest 没有 source 合约。其 iOS 26 SBS 返回可能稳定地说谎（实测
            # 被测 App 已在前台仍返回 Settings），不能继续当作精准模式的输入。
            raise _ForegroundUnsupported("legacy guest omitted frontmost source")
        if source == "unknown":
            raise _ForegroundUnsupported(
                "guest reports frontmost source=unknown (SBS unavailable)",
            )
        return str(resp.get("bundle_id") or "")

    def _refocus(
        self,
        bundle_id: str,
        sock: str,
        *,
        prev: str | None,
        degraded: bool,
        springboard_icon_preview: tuple[int, int] | None = None,
    ) -> None:
        try:
            self._uiopen(bundle_id, sock)
        except Exception as exc:  # noqa: BLE001
            self._note("error", f"uiopen failed: {exc}")
            return
        if degraded:
            # 探活说目标没在跑，uiopen 冷启拉起。
            self._note("ensured", "uiopen relaunch (degraded: not running)")
            return

        # 精准模式必回读校验：前台读数在 iOS 26 上可能是启发式误报（恒报
        # Spotlight 等），不校验就会每拍 uiopen 把前台 app 打得反复失焦。
        try:
            time.sleep(1.0)
            fg = self._foreground_bundle(sock)
            if fg != bundle_id and springboard_icon_preview is not None:
                self._tap_springboard_icon(springboard_icon_preview, sock)
                time.sleep(1.0)
                fg = self._foreground_bundle(sock)
        except Exception as exc:  # noqa: BLE001
            self._note("error", f"foreground verify failed after uiopen: {exc}")
            return
        if fg == bundle_id:
            self._fg_mismatch_streak = 0
            self._note("refocused", f"was {prev or 'home'!r}")
            return
        # uiopen 后读数仍非目标：uiopen 失效，或前台读数在说谎。目标确认在跑
        # 且连续两拍如此 → 判定后者，进入有限频受限恢复，停止每拍 flap。
        self._fg_mismatch_streak += 1
        if self._fg_mismatch_streak >= 2 and self._is_running(bundle_id, sock) is True:
            self._fg_supported = False
            # 已确认刚刚的精准读数不可信；从此刻起按降级冷却再探，不能在下一拍
            # 立即重新相信同一条坏读数并再次激活 app。
            now = time.monotonic()
            self._degraded_last_probe = now
            self._degraded_last_uiopen = now
            logger.warning(
                "app_foreground 读数连续 %d 次在 uiopen 后仍报 %r（目标 %s 在跑）——"
                "前台信号不可信，前台保活进入有限频受限恢复",
                self._fg_mismatch_streak, fg or "home", bundle_id)
            self._note("degraded", f"foreground signal untrusted (still {fg or 'home'!r} after uiopen)")
            return
        self._note("error", f"uiopen/icon fallback still foreground={fg or 'home'!r}")

    def _uiopen(self, bundle_id: str, sock: str) -> None:
        import shlex

        from autophone.device.shell import run_shell

        r = run_shell(f"uiopen --bundleid {shlex.quote(bundle_id)}",
                      timeout_ms=15_000, sock_path=sock)
        if r.code != 0:
            raise RuntimeError(f"uiopen exit {r.code}: {r.stderr.strip()!r}")

    def _tap_springboard_icon(self, preview_xy: tuple[int, int], sock: str) -> None:
        from autophone.device.uds import preview_to_native
        from autophone.device.uds import request as uds_request

        uds_request({"t": "key", "name": "home", "screen": False}, sock_path=sock, timeout=8)
        time.sleep(0.5)
        x, y = preview_xy
        nx, ny = preview_to_native(int(x), int(y))
        uds_request({"t": "tap", "x": nx, "y": ny, "screen": False}, sock_path=sock, timeout=8)

    def _is_running(self, bundle_id: str, sock: str) -> bool | None:
        """Best-effort process presence probe（True/False/None=无结论）。

        降级模式用它区分「冷启动」和「后台进程拉回」。注意当前 VM build 在主屏时
        app_list 可能整体为空（误报 False），所以两类恢复均受冷却窗口限制，避免
        keeper 退化回每拍 uiopen。
        """
        from autophone.device.uds import request as uds_request

        try:
            resp, _ = uds_request({"t": "app_list", "filter": "running"}, sock_path=sock, timeout=8)
        except Exception as exc:  # noqa: BLE001
            self._note("error", f"app_list probe failed: {exc}")
            return None
        if not resp.get("ok", True):
            self._note("error", f"app_list failed: {resp.get('error')!r}")
            return None
        for app in resp.get("apps") or []:
            if app.get("bundle_id") == bundle_id and int(app.get("pid") or 0) > 0:
                return True
        return False

    def _degraded_tick(
        self,
        bundle_id: str,
        sock: str,
        springboard_icon_preview: tuple[int, int] | None,
    ) -> None:
        """降级拍：有限频地尝试拉回，但绝不把 pid 存在伪报为前台成功。"""
        running = self._is_running(bundle_id, sock)
        if running is None:
            # 探活无结论：宁可不保活也不盲打（_is_running 已 note error）。
            return
        now = time.monotonic()
        if now - self._degraded_last_uiopen < self.degraded_cooldown_s:
            self._note(
                "cooldown",
                f"foreground unverified; retry in {self.degraded_cooldown_s:.0f}s cooldown",
            )
            return
        self._degraded_last_uiopen = now
        try:
            self._uiopen(bundle_id, sock)
            # 图标兜底自带 Home 键：目标存活时可能正在前台，Home 会主动把它按回
            # 桌面（模块 docstring「闪动源」取舍）。只有确认进程不存在才允许走。
            if running is False and springboard_icon_preview is not None:
                self._tap_springboard_icon(springboard_icon_preview, sock)
        except Exception as exc:  # noqa: BLE001 - 看门狗 fail-soft
            self._note("error", f"degraded foreground recovery failed: {exc}")
            return
        if running is False:
            self._note("ensured", "uiopen relaunch (degraded: not running; foreground unverified)")
            return
        self._note(
            "unverified_refocus",
            "target process running; issued uiopen only (no Home/icon while possibly foreground)",
        )

    def _strong_tick(
        self,
        bundle_id: str,
        sock: str,
        springboard_icon_preview: tuple[int, int] | None,
    ) -> None:
        """强保活：只要不能确认目标仍在前台，就本拍拉回，不受降级冷却限制。

        「拉回」在降级下限定为 uiopen；Home+图标只用于进程确认不存在的冷启。
        """
        if self._fg_supported is not False:
            try:
                fg = self._foreground_bundle(sock)
            except _ForegroundUnsupported:
                self._fg_supported = False
            except Exception as exc:  # noqa: BLE001 - 强保活仍尝试恢复
                logger.debug("强保活前台探针失败，继续拉回: %s", exc)
            else:
                self._fg_supported = True
                if fg == bundle_id:
                    self._fg_mismatch_streak = 0
                    self._note("ok", "already foreground (strong)")
                    return
                # 前台读数仍可信时沿用原来的回读校验与图标兜底；只有明确降级后
                # 才切到下面的无冷却强拉回，避免混淆精准状态与用户选定模式。
                self._refocus(
                    bundle_id,
                    sock,
                    prev=fg,
                    degraded=False,
                    springboard_icon_preview=springboard_icon_preview,
                )
                return
        # 降级强保活：每拍发 uiopen（对前台 app 是 no-op，安全）；图标兜底带 Home
        # 键，目标存活时可能正打断真前台，只在确认进程不存在时才走（闪动源取舍）。
        running = self._is_running(bundle_id, sock)
        try:
            self._uiopen(bundle_id, sock)
            if running is False and springboard_icon_preview is not None:
                self._tap_springboard_icon(springboard_icon_preview, sock)
        except Exception as exc:  # noqa: BLE001 - 看门狗 fail-soft
            self._note("error", f"strong foreground recovery failed: {exc}")
            return
        if running is False:
            self._note(
                "strong_refocus",
                "strong mode relaunched target via uiopen/icon (was not running)",
            )
            return
        self._note(
            "strong_refocus",
            "strong mode issued uiopen only (no Home/icon while possibly foreground)",
        )

    def _retry_precise_foreground(self, sock: str) -> tuple[bool, str]:
        """降级后有限频重探 guest；升级后的可信 source 出现即可恢复精准模式。"""
        now = time.monotonic()
        if now - self._degraded_last_probe < self.degraded_cooldown_s:
            return False, ""
        self._degraded_last_probe = now
        try:
            fg = self._foreground_bundle(sock)
        except _ForegroundUnsupported:
            return False, ""
        except Exception as exc:  # noqa: BLE001 - 这拍没有可用能力，继续安全降级
            logger.debug("降级模式重探 app_foreground 失败: %s", exc)
            return False, ""
        self._fg_supported = True
        self._fg_mismatch_streak = 0
        logger.info("guest app_foreground 已恢复可信 source，前台保活回到精准模式")
        return True, fg

    def _tick(self) -> None:
        with self._mu:
            target = dict(self._target) if self._target else None
        if not target:
            return
        device = target["device"]
        bundle_id = target["bundle_id"]
        springboard_icon_preview = target.get("springboard_icon_preview")
        selected_mode = target.get("mode", "strong")

        # 回归运行占用设备时暂停保活，不抢锁。
        if self._device_busy(device):
            self._note("paused", f"device {device} locked by run")
            return

        sock = self._sock()

        if selected_mode == "strong":
            self._strong_tick(bundle_id, sock, springboard_icon_preview)
            return

        # 降级模式：前台读数不可用时，先有限频重探能力。guest 更新后 source 变可信
        # 会自动回到精准路径；否则以有限频 uiopen/图标尝试拉回，绝不伪报「已前台」。
        if self._fg_supported is False:
            recovered, fg = self._retry_precise_foreground(sock)
            if recovered:
                if fg == bundle_id:
                    self._note("ok", "already foreground (precise capability recovered)")
                    return
                self._refocus(
                    bundle_id,
                    sock,
                    prev=fg,
                    degraded=False,
                    springboard_icon_preview=springboard_icon_preview,
                )
                return
            self._degraded_tick(bundle_id, sock, springboard_icon_preview)
            return

        # 精准模式：读到前台 bundle，只在不是目标时才拉回（省去无谓 uiopen）。
        try:
            fg = self._foreground_bundle(sock)
        except _ForegroundUnsupported as exc:
            self._fg_supported = False
            logger.info("guest 不支持可信 app_foreground（guest build 太旧或 iOS 26 SBS API 失效），"
                        "前台保活进入有限频受限恢复。需上游 vphone-cli "
                        "实现 RBS/FBS 替代才能启用精准模式: %s", exc)
            self._degraded_tick(bundle_id, sock, springboard_icon_preview)
            return
        except Exception as exc:  # noqa: BLE001 - 设备不可达：跳过这拍，别空转 uiopen
            self._note("error", f"foreground probe failed: {exc}")
            return

        self._fg_supported = True
        if fg == bundle_id:
            self._fg_mismatch_streak = 0
            self._note("ok", "already foreground")
            return
        # 前台不是目标：拉回或冷启动目标 app。`uiopen --bundleid` 是 SpringBoard
        # 语义，目标已在前台时 no-op，未运行时会启动；这正是 dashboard 的「自动拉起」语义。
        self._refocus(
            bundle_id,
            sock,
            prev=fg,
            degraded=False,
            springboard_icon_preview=springboard_icon_preview,
        )
