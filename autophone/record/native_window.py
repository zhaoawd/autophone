"""Read real window geometry; convert desktop points to canonical preview coordinates."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from autophone.device.uds import EXPECTED_PREVIEW, preview_to_native


class NativeWindowError(ValueError):
    pass


def helper_path() -> Path:
    if sys.platform != "darwin":
        raise NativeWindowError("native recording requires macOS")
    source = Path(__file__).with_suffix(".swift")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = Path(tempfile.gettempdir()) / f"autophone-native-window-{os.getuid()}" / digest
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "window-probe"
    if not binary.exists():
        temporary = cache / f"window-probe-{uuid.uuid4().hex}"
        try:
            result = subprocess.run(["swiftc", "-module-cache-path", str(cache / "modules"),
                str(source), "-o", str(temporary)], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            temporary.unlink(missing_ok=True)
            raise NativeWindowError("native helper compilation timed out") from exc
        if result.returncode:
            raise NativeWindowError("native helper compilation failed: " + result.stderr[-2000:])
        temporary.replace(binary)
    return binary


def probe(config: str | Path, *, window_id: int | None = None) -> dict[str, Any]:
    command = [str(helper_path()), "--config", str(Path(config).resolve())]
    if window_id is not None:
        command += ["--window-id", str(window_id)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise NativeWindowError("native probe timed out") from exc
    try:
        rows = [json.loads(line) for line in result.stdout.splitlines()]
    except json.JSONDecodeError as exc:
        raise NativeWindowError("invalid native probe output") from exc
    if result.returncode or not rows or not isinstance(rows[-1], dict) or rows[-1].get("type") != "ready":
        raise NativeWindowError(str(rows[-1] if rows else "native probe failed"))
    snapshot = rows[-1]["snapshot"]
    WindowGeometry.from_snapshot(snapshot)
    return snapshot


@dataclass(frozen=True)
class WindowGeometry:
    content: tuple[float, float, float, float]
    backing_scale: float

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> WindowGeometry:
        content = snapshot.get("content")
        scale = (snapshot.get("display") or {}).get("backing_scale")
        if (not isinstance(content, list) or len(content) != 4
                or any(type(x) not in (float, int) or not math.isfinite(x) for x in content)
                or type(scale) not in (float, int) or not math.isfinite(scale) or scale <= 0):
            raise NativeWindowError("invalid content geometry")
        _, _, width, height = content
        if width < 86 or height < 186 or abs(width / height / (430 / 932) - 1) >= .003:
            raise NativeWindowError("content aspect mismatch")
        return cls(tuple(content), float(scale))

    def preview(self, point: list[float]) -> list[int]:
        if len(point) != 2 or any(type(v) not in (float, int) or not math.isfinite(v) for v in point):
            raise NativeWindowError("invalid desktop point")
        x, y, width, height = self.content
        if not x <= point[0] < x + width or not y <= point[1] < y + height:
            raise NativeWindowError("pointer outside content")
        return [int((point[0] - x) / width * EXPECTED_PREVIEW[0]),
                int((point[1] - y) / height * EXPECTED_PREVIEW[1])]

    def native(self, point: list[float]) -> list[int]:
        return list(preview_to_native(*self.preview(point)))

    def desktop(self, px: int, py: int) -> list[float]:
        if not 0 <= px < EXPECTED_PREVIEW[0] or not 0 <= py < EXPECTED_PREVIEW[1]:
            raise NativeWindowError("preview outside geometry")
        x, y, width, height = self.content
        return [x + (px + .5) / EXPECTED_PREVIEW[0] * width,
                y + (py + .5) / EXPECTED_PREVIEW[1] * height]
