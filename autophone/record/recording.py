"""平台 run 期录屏（path A 预览录制 · 后台线程 · 可选证据）。

phase2 用例在 ScreenLock execute 段跑的同时，后台线程轮询 UDS screenshot 攒帧（默认 4fps）；
run 收尾发停录信号，ffmpeg 变帧率 mux 成 `<run_dir>/recording.mp4`（真实墙钟速率回放）。
复用 phase1 `screen_record` 的 Frame / 变帧率 mux 原语，只把「定长录制」换成「录到停」。

advisory 侧证据，采集与编码失败 **fail-open**（与 api capture 同纪律）：禁用 / sock 不可达 / 零帧 /
几何漂移 / 无 ffmpeg / mux 超时 → 无 mp4，绝不拖垮回归。录屏是设备画面 → artifact 标
sensitivity=sensitive、share_policy=forbidden（同截图治理，禁止外发）。

开关：`AUTOPHONE_RECORD=0/off/false` 关闭；`AUTOPHONE_RECORD_FPS`（默认 4）调采样率。
线程内采集异常记入 `self.error`。停止阶段必须确认线程已退出；超时继续等待并保留调用方执行锁。
RunService 延后 mux 到 collect；独立 context manager 调用仍在退出时完成 mux。
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, Callable

RECORDING_REL = "recording.mp4"
FRAMES_REL = "planes/ui/recording"
_MUX_TIMEOUT_S = 60.0
_JOIN_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class FrozenFrame:
    path: Path
    t_rel: float


@dataclass(frozen=True)
class FrozenRecording:
    frames: tuple[FrozenFrame, ...]
    geometry: tuple[int, int] | None
    cutoff: float


_DEFERRED: ContextVar[list | None] = ContextVar("deferred_recording_materialization", default=None)


@contextmanager
def defer_materialization():
    """Collect stopped recordings for the caller's Host-only collection phase."""
    existing = _DEFERRED.get()
    if existing is not None:
        yield existing
        return
    pending = []
    token = _DEFERRED.set(pending)
    try:
        yield pending
    finally:
        _DEFERRED.reset(token)


def recording_enabled() -> bool:
    return os.environ.get("AUTOPHONE_RECORD", "").strip().lower() not in ("0", "off", "false", "no")


def _max_fps() -> float:
    try:
        v = float(os.environ.get("AUTOPHONE_RECORD_FPS", "4"))
        return v if v > 0 else 4.0
    except ValueError:
        return 4.0


class BackgroundRecorder:
    """录到停的后台预览录制器。`__enter__` 起线程攒帧，`__exit__` 停录并 mux。

    `capture_loop` 可注入（测试喂合成帧，免 VM/ffmpeg）。无论用例段是否抛异常，`__exit__`
    都先停录再 mux 已攒到的帧（失败回放也拿得到画面），且永不吞回归异常。"""

    def __init__(
        self,
        run_dir: Path,
        *,
        sock: str | None = None,
        max_fps: float | None = None,
        capture_loop: Callable[["BackgroundRecorder"], None] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.sock = sock
        self.max_fps = max_fps if max_fps is not None else _max_fps()
        self.out_dir: Path = self.run_dir / FRAMES_REL
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture_loop = capture_loop
        self.frames: list[Any] = []
        self.geometry: tuple[int, int] | None = None
        self.mp4: Path | None = None
        self.error: str | None = None
        self._frozen: FrozenRecording | None = None
        self._materialized = False
        self._stage_binding = None

    def __enter__(self) -> "BackgroundRecorder":
        if self._thread is not None or self._frozen is not None:
            raise RuntimeError("recording producer cannot be restarted")
        from autophone.device.stage_guard import StageGuard, OperationClass
        self._stage_binding = StageGuard.producer_binding(frozenset({OperationClass.READ}))
        context = copy_context()
        def capture():
            with StageGuard.bind(self._stage_binding):
                self._capture()
        self._thread = threading.Thread(target=lambda: context.run(capture), name="autophone-rec", daemon=True)
        self._thread.start()
        return self

    def _capture(self) -> None:
        try:
            if self._capture_loop is not None:
                self._capture_loop(self)
            else:
                self._default_capture()
        except Exception as e:  # noqa: BLE001 录屏不拖垮回归
            self.error = f"{type(e).__name__}: {e}"

    def _default_capture(self) -> None:
        from autophone.device.uds import GeometryDrift, GeometryGuard, request
        from autophone.device.screen_record import Frame

        if self.sock is None:
            from autophone.execution.engine.actions import resolve_sock
            self.sock = resolve_sock()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        guard = GeometryGuard()
        min_interval = (1.0 / self.max_fps) if self.max_fps > 0 else 0.0
        clock = time.perf_counter
        t0 = clock()
        last: float | None = None
        while not self._stop.is_set():
            if min_interval and last is not None:
                wait = min_interval - (clock() - last)
                if wait > 0 and self._stop.wait(wait):
                    break
            try:
                obj, sample = request({"t": "screenshot"}, sock_path=self.sock, timeout=10)
            except Exception:  # noqa: BLE001 瞬时不可达 → 退避重试到停录
                if self._stop.wait(0.5):
                    break
                continue
            if not obj.get("ok") or not obj.get("image"):
                if self._stop.wait(0.5):
                    break
                continue
            try:
                guard.check(sample)
            except GeometryDrift:
                break  # 几何漂移 → 停录，只 mux 漂移前的同尺寸帧（避免混尺寸损坏）
            last = clock()
            fp = self.out_dir / f"frame_{len(self.frames) + 1:05d}.jpg"
            fp.write_bytes(base64.b64decode(obj["image"]))
            self.frames.append(Frame(fp, last - t0))
        self.geometry = guard.locked

    def freeze(self) -> FrozenRecording:
        """Stop the producer before exposing its immutable frame manifest.

        A slow producer retains the caller's ScreenLock until it actually exits.
        This does not claim a VM quiesce or a Rig Generation transition.
        """
        if self._frozen is not None:
            return self._frozen
        self._stop.set()
        if self._stage_binding is not None:
            self._stage_binding.guard.revoke_producer(self._stage_binding)
        interrupted: BaseException | None = None
        if self._thread is not None:
            try:
                self._thread.join(timeout=_JOIN_TIMEOUT_S)
            except BaseException as exc:
                interrupted = exc
            if self._thread.is_alive():
                logging.getLogger(__name__).error(
                    "recording producer has not stopped; retaining execution ownership"
                )
            while self._thread.is_alive():
                try:
                    self._thread.join(timeout=0.1)
                except BaseException as exc:
                    interrupted = exc
        self._frozen = FrozenRecording(
            tuple(FrozenFrame(frame.path, frame.t_rel) for frame in self.frames),
            self.geometry, time.monotonic(),
        )
        if interrupted is not None:
            raise interrupted
        return self._frozen

    def materialize(self, frozen: FrozenRecording) -> None:
        """Mux only the manifest returned after this producer stopped."""
        if frozen is not self._frozen or (self._thread is not None and self._thread.is_alive()):
            raise ValueError("recording is not frozen by this producer")
        if self._materialized:
            return
        try:
            self._mux()
        except Exception as e:  # noqa: BLE001 mux remains optional evidence
            self.error = f"mux: {type(e).__name__}: {e}"
        self._materialized = True

    def export_frozen(self, frozen: FrozenRecording, *, root: Path) -> dict[str, Any]:
        """Describe stopped recording inputs without retaining a producer object."""
        if frozen is not self._frozen or (self._thread is not None and self._thread.is_alive()):
            raise ValueError("recording is not frozen by this producer")
        root = root.resolve()
        frames = []
        for frame in frozen.frames:
            path = frame.path.resolve()
            relative = path.relative_to(root)
            data = path.read_bytes()
            frames.append({"path": str(relative), "t_rel": frame.t_rel,
                           "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return {"schema_version": "frozen-recording/v1",
                "run_dir": str(self.run_dir.resolve().relative_to(root)),
                "frames": frames, "geometry": list(frozen.geometry) if frozen.geometry else None,
                "cutoff": frozen.cutoff, "error": self.error}

    @classmethod
    def restore_frozen(cls, manifest: dict[str, Any], *, root: Path) -> tuple["BackgroundRecorder", FrozenRecording]:
        """Verify persisted inputs and create a Host-only, non-restartable recorder."""
        if manifest.get("schema_version") != "frozen-recording/v1":
            raise ValueError("unsupported frozen recording version")
        root = root.resolve()

        def local_path(value: str) -> Path:
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("recording input must be relative to the run directory")
            path = (root / relative).resolve()
            path.relative_to(root)
            return path

        frames = []
        for item in manifest["frames"]:
            path = local_path(item["path"])
            data = path.read_bytes()
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError(f"frozen recording input changed: {item['path']}")
            frames.append(FrozenFrame(path, item["t_rel"]))
        recorder = cls(local_path(manifest["run_dir"]))
        geometry = manifest["geometry"]
        frozen = FrozenRecording(tuple(frames), tuple(geometry) if geometry else None, manifest["cutoff"])
        recorder._frozen = frozen
        recorder.error = manifest["error"]
        return recorder, frozen

    def __exit__(self, exc_type, exc, tb) -> bool:
        frozen = self.freeze()
        pending = _DEFERRED.get()
        if pending is None:
            self.materialize(frozen)
        else:
            pending.append((self, frozen))
        return False

    def _mux(self) -> None:
        if self._frozen is None:
            raise ValueError("recording must be frozen before mux")
        frames = self._frozen.frames
        if not frames:
            return
        import shutil

        from autophone.device.screen_record import ffmpeg_mux_cmd, frame_durations, write_concat_file

        durs = frame_durations(frames)
        list_path = self.out_dir / "frames.concat"
        write_concat_file(frames, durs, list_path)
        if shutil.which("ffmpeg") is None:
            return  # 帧 + concat 已落，可事后手动 mux；无 mp4 → 看板不渲染录屏区
        out = self.run_dir / RECORDING_REL
        subprocess.run(ffmpeg_mux_cmd(list_path, out), check=True, capture_output=True, timeout=_MUX_TIMEOUT_S)
        self.mp4 = out


def build_recorder(run_dir: Path, spec: dict[str, Any]) -> BackgroundRecorder | None:
    """按开关构造录制器；关闭 → None（调用方退回 nullcontext，行为不变）。"""
    if not recording_enabled():
        return None
    return BackgroundRecorder(Path(run_dir))
