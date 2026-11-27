"""Read the device preferences that affect text rendering, without changing them."""

from __future__ import annotations

import plistlib
import tempfile
from pathlib import Path
from typing import Any, Callable

from autophone.observation.vphone_text_size import VphoneTextSizeProbe

# iOS 出厂态。设备从没开过粗体文本时偏好文件里根本没有这个键，文件本身也可能不存在。
ACCESSIBILITY_PREFERENCES_PATH = (
    "/var/mobile/Library/Preferences/com.apple.Accessibility.plist"
)
BOLD_TEXT_KEY = "EnhancedTextLegibilityEnabled"
DEFAULT_BOLD_TEXT = False

TEXT_SIZE_CATEGORIES = frozenset(
    "UICTContentSizeCategory" + suffix
    for suffix in (
        "XS", "S", "M", "L", "XL", "XXL", "XXXL", "AccessibilityM",
        "AccessibilityL", "AccessibilityXL", "AccessibilityXXL", "AccessibilityXXXL",
    )
)


def validate_rendering_environment(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"text_size_category", "bold_text"}
        or not isinstance(value["text_size_category"], str)
        or value["text_size_category"] not in TEXT_SIZE_CATEGORIES
        or type(value["bold_text"]) is not bool
    ):
        raise ValueError(
            "environment must declare a supported text_size_category and boolean bold_text",
        )
    return dict(value)


class VphoneRenderingEnvironmentProbe:
    """Read Dynamic Type and Bold Text preferences through the selected UDS socket.

    Bold Text 的缺失处理与 `VphoneTextSizeProbe` 同一口径：先用 `run_shell` 做存在性
    探测区分 present/absent，**文件不存在或键缺失回落出厂态**（`bold_text=False`），
    只有"探针链路本身坏了"才抛错——存在性探测给不出 present/absent、或探测说文件
    present 而 `file_get` 取不回来。值存在但不是 0/1/bool 时同样抛错，错误信息把
    "无法读取"与"无效值"分开，便于定位是链路问题还是设备状态问题。
    """

    def __init__(
        self,
        *,
        sock_path: str | None = None,
        timeout: float = 30.0,
        request_fn: Callable[..., Any] | None = None,
        shell_fn: Callable[[str], Any] | None = None,
        exists_fn: Callable[[], bool] | None = None,
        text_size_probe: Callable[[], str] | None = None,
    ) -> None:
        self.sock_path = sock_path
        self.timeout = timeout
        self.request_fn = request_fn
        self.shell_fn = shell_fn
        self.exists_fn = exists_fn
        self.text_size_probe = text_size_probe or VphoneTextSizeProbe(
            sock_path=sock_path, timeout=timeout, request_fn=request_fn,
            shell_fn=shell_fn,
        )

    def _preferences_exist(self) -> bool:
        if self.exists_fn is not None:
            # 没有 shell 通道的采集面（如无图采集）用这个 seam 声明存在性判定，
            # 由调用方决定"读不回来"算拒绝还是算出厂态。
            return bool(self.exists_fn())
        shell_fn = self.shell_fn
        if shell_fn is None:
            from autophone.device.shell import run_shell

            def shell_fn(command: str) -> Any:
                return run_shell(command, sock_path=self.sock_path)

        try:
            result = shell_fn(
                f'[ -f "{ACCESSIBILITY_PREFERENCES_PATH}" ] && echo present || echo absent',
            )
        except Exception as exc:  # noqa: BLE001 — 探针边界
            raise ValueError(
                f"cannot probe {ACCESSIBILITY_PREFERENCES_PATH}: {exc}",
            ) from exc
        answer = str(getattr(result, "stdout", result) or "").strip()
        if answer not in ("present", "absent"):
            # 分辨不出"文件不在"和"探针坏了"时不能替设备回答出厂态：
            # 那正好会在真出问题的时候放行。
            raise ValueError(
                f"inconclusive existence probe for {ACCESSIBILITY_PREFERENCES_PATH}: "
                f"{answer!r}",
            )
        return answer == "present"

    def _bold_text(self) -> bool:
        if not self._preferences_exist():
            return DEFAULT_BOLD_TEXT
        request_fn = self.request_fn
        if request_fn is None:
            from autophone.device.uds import request as request_fn
        with tempfile.TemporaryDirectory(prefix="autophone-environment-") as workspace:
            local = Path(workspace) / "accessibility.plist"
            response, _ = request_fn(
                {
                    "t": "file_get",
                    "path": ACCESSIBILITY_PREFERENCES_PATH,
                    "save": str(local),
                },
                timeout=self.timeout,
                **({"sock_path": self.sock_path} if self.sock_path else {}),
            )
            if (
                not isinstance(response, dict)
                or response.get("ok") is not True
                or not local.is_file()
            ):
                raise ValueError(
                    f"cannot read Bold Text environment preference from "
                    f"{ACCESSIBILITY_PREFERENCES_PATH}: {response!r}",
                )
            try:
                preferences = plistlib.loads(local.read_bytes())
            except (plistlib.InvalidFileException, ValueError) as exc:
                raise ValueError(
                    f"cannot parse {ACCESSIBILITY_PREFERENCES_PATH}: {exc}",
                ) from exc
        if not isinstance(preferences, dict):
            raise ValueError(
                f"{ACCESSIBILITY_PREFERENCES_PATH} is not a plist dictionary",
            )
        if BOLD_TEXT_KEY not in preferences:
            return DEFAULT_BOLD_TEXT
        bold = preferences[BOLD_TEXT_KEY]
        if type(bold) not in (int, bool) or bold not in (0, 1):
            raise ValueError(f"invalid {BOLD_TEXT_KEY} value: {bold!r}")
        return bool(bold)

    def __call__(self) -> dict[str, Any]:
        category = self.text_size_probe()
        return validate_rendering_environment({
            "text_size_category": category, "bold_text": self._bold_text(),
        })
