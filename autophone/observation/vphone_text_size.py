"""设备 Dynamic Type 档位的读取。

RN 的 `Text` 默认 `allowFontScaling=true`，字号随系统 Dynamic Type 档位缩放，
`XL` 相对默认 `L` 是 ×1.118。这个缩放**只影响内在尺寸的元素**——显式写死 height 的
框一动不动，所以症状只在少数元素上冒头（2026-08-22 vm：price frame 48×29 →
53.67×32.67、eta x 103→108.67），极易被读成"这个构建把某个组件做大了"。

档位不在 `env_fingerprint`（`vm/430x932/en-US/light`）里，也没有任何观测通道会
报它，所以采集侧必须自己去读：读不到或不是声明的档位，这次采集就不构成对构建的判定。
"""

from __future__ import annotations

import plistlib
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# iOS 出厂档位。设备从没改过字号时 UIKit 偏好文件里根本没有这个键。
DEFAULT_TEXT_SIZE_CATEGORY = "UICTContentSizeCategoryL"
PREFERENCES_PATH = "/var/mobile/Library/Preferences/com.apple.UIKit.plist"
PREFERENCE_KEY = "UIPreferredContentSizeCategoryName"


class TextSizeProbeError(RuntimeError):
    """探针本身没跑通——与"读到了一个不合口径的档位"分开。"""


class VphoneTextSizeProbe:
    """经 UDS 读 guest 的 UIKit 偏好，返回 `UICTContentSizeCategory*`。

    guest 里没有 `defaults` / `plutil`，所以把二进制 plist 取回宿主用 plistlib 解析。
    """

    def __init__(
        self,
        *,
        sock_path: str | None = None,
        timeout: float = 30.0,
        request_fn: Callable[..., tuple[dict[str, Any], Any]] | None = None,
        shell_fn: Callable[[str], Any] | None = None,
    ) -> None:
        self.sock_path = sock_path
        self.timeout = timeout
        self._request_fn = request_fn
        self._shell_fn = shell_fn

    def _request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request_fn = self._request_fn
        if request_fn is None:
            from autophone.device.uds import request as request_fn  # noqa: PLC0415
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.sock_path is not None:
            kwargs["sock_path"] = self.sock_path
        response, _sample = request_fn(dict(payload), **kwargs)
        if not isinstance(response, dict):
            raise TextSizeProbeError(f"unexpected UDS response: {response!r}")
        return response

    def _preferences_exist(self) -> bool:
        shell_fn = self._shell_fn
        if shell_fn is None:
            from autophone.device.shell import run_shell  # noqa: PLC0415

            def shell_fn(command: str) -> Any:
                return run_shell(command, sock_path=self.sock_path)

        try:
            result = shell_fn(
                f'[ -f "{PREFERENCES_PATH}" ] && echo present || echo absent',
            )
        except Exception as exc:  # noqa: BLE001 — 探针边界
            raise TextSizeProbeError(f"cannot probe {PREFERENCES_PATH}: {exc}") from exc
        answer = str(getattr(result, "stdout", result) or "").strip()
        if answer not in ("present", "absent"):
            # 分辨不出"文件不在"和"探针坏了"时不能替设备回答默认档位：
            # 那正好会在真出问题的时候放行。
            raise TextSizeProbeError(
                f"inconclusive existence probe for {PREFERENCES_PATH}: {answer!r}",
            )
        return answer == "present"

    def __call__(self) -> str:
        if not self._preferences_exist():
            return DEFAULT_TEXT_SIZE_CATEGORY
        with tempfile.TemporaryDirectory(prefix="autophone-text-size-") as workspace:
            local = Path(workspace) / "com.apple.UIKit.plist"
            response = self._request({
                "t": "file_get",
                "path": PREFERENCES_PATH,
                "save": local.as_posix(),
            })
            if response.get("ok") is not True or not local.is_file():
                raise TextSizeProbeError(
                    f"cannot read {PREFERENCES_PATH}: {response!r}",
                )
            try:
                preferences = plistlib.loads(local.read_bytes())
            except (plistlib.InvalidFileException, ValueError) as exc:
                raise TextSizeProbeError(
                    f"cannot parse {PREFERENCES_PATH}: {exc}",
                ) from exc
        if not isinstance(preferences, dict):
            raise TextSizeProbeError(f"{PREFERENCES_PATH} is not a plist dictionary")
        category = preferences.get(PREFERENCE_KEY, DEFAULT_TEXT_SIZE_CATEGORY)
        if not isinstance(category, str) or not category.startswith(
            "UICTContentSizeCategory",
        ):
            raise TextSizeProbeError(
                f"unexpected {PREFERENCE_KEY} value: {category!r}",
            )
        return category
