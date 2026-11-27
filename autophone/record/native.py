"""Passive native pointer producer. No input dispatch; stop joins before lease release."""
from __future__ import annotations

from collections import deque
import json
import math
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from autophone.device.uds import EXPECTED_PREVIEW, preview_to_native
from autophone.record.native_window import NativeWindowError, WindowGeometry, helper_path


def pointer_intent(observation: dict[str, Any]) -> dict[str, Any]:
    """Validate the helper's complete gesture and project logical desktop points."""
    if observation.get("source") != "cgeventtap_listen_only" or observation.get("type") != "gesture":
        raise NativeWindowError("unrecognized native input source")
    snapshot = observation["snapshot"]
    if (type(snapshot.get("pid")) is not int or snapshot["pid"] <= 0
            or type(snapshot.get("window_id")) is not int or snapshot["window_id"] <= 0
            or not Path(snapshot.get("config") or "").is_absolute()
            or snapshot.get("content_source") != "ax_toolbar_boundary"):
        raise NativeWindowError("native window identity missing")
    geometry = WindowGeometry.from_snapshot(snapshot)
    down, up = observation["down"], observation["up"]
    times = [down["uptime"], up["uptime"]]
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in times) or not 0 <= times[1] - times[0] <= 30:
        raise NativeWindowError("invalid native gesture duration")
    start, end = geometry.preview(down["point"]), geometry.preview(up["point"])
    samples = observation.get("samples")
    if not isinstance(samples, list) or not 2 <= len(samples) <= 512 or samples[0] != down["point"] or samples[-1] != up["point"]:
        raise NativeWindowError("incomplete native gesture samples")
    for point in samples:
        geometry.preview(point)
    distance = max(math.dist(down["point"], point) for point in samples)
    if not math.isclose(distance, observation["max_distance"], abs_tol=.01):
        raise NativeWindowError("native gesture distance mismatch")
    # Threshold is expressed in canonical preview points, independent of host DPI.
    if distance * 430 / geometry.content[2] <= 6:
        if times[1] - times[0] > 1:
            raise NativeWindowError("long press cannot be represented as tap")
        return {"kind": "tap", "preview": start}
    return {"kind": "swipe", "from_preview": start, "to_preview": end,
            "ms": max(1, round((times[1] - times[0]) * 1000))}


def observed_event_valid(event: dict[str, Any]) -> bool:
    """Permit native pointer intent into draft IR without inventing a UDS ack."""
    try:
        obs = event["input_observation"]
        intent = pointer_intent(obs)
        age = obs["down"]["uptime"] - obs["before_capture_uptime"]
        if (obs.get("delivery") != "unverified" or not 0 <= age <= 1
                or not math.isclose(age, obs["before_frame_age_s"], abs_tol=.0001)
                or event.get("uds_result") != {"dispatched": False}):
            return False
        if any(event.get(key) != value for key, value in intent.items()):
            return False
        if intent["kind"] == "tap":
            return event.get("native") == list(preview_to_native(*intent["preview"]))
        return (event.get("from_native") == list(preview_to_native(*intent["from_preview"]))
                and event.get("to_native") == list(preview_to_native(*intent["to_preview"])))
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False


class NativePointerRecorder:
    """Own helper, bounded pre-input frame cache and one serial capture worker."""

    def __init__(self, session: Any, config: Path, commit: Callable[..., None], fault: Callable[..., None],
                 *, popen: Callable[..., Any] = subprocess.Popen,
                 ready_timeout_s: float = 10, heartbeat_timeout_s: float = 3) -> None:
        self.session, self.config, self.commit, self.fault = session, config, commit, fault
        self._popen = popen
        self.ready_timeout_s, self.heartbeat_timeout_s = ready_timeout_s, heartbeat_timeout_s
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._freeze_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._process: Any = None
        self.error: str | None = None
        self.snapshot: dict[str, Any] | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        binary = helper_path()
        self._process = self._popen([str(binary), "--config", str(self.config), "--listen"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=False)
        self._worker = threading.Thread(target=self._run, name="native-pointer-recorder", daemon=True)
        self._worker.start()
        if not self._ready.wait(self.ready_timeout_s + 15) or self.error or self.snapshot is None:
            self.stop()
            raise NativeWindowError(self.error or "native recorder start timed out")

    def _run(self) -> None:
        frames: deque = deque(maxlen=16)
        last_up = float("-inf")
        last_capture = float("-inf")
        last_message = time.monotonic()
        buffer = b""
        stopped = False
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self._process.stdout, selectors.EVENT_READ)
                while not self._stop.is_set():
                    for key, _ in selector.select(.03):
                        chunk = key.fileobj.read1(65536)
                        if not chunk:
                            if not stopped:
                                raise NativeWindowError("native helper exited before stop acknowledgement")
                            return
                        buffer += chunk
                        if len(buffer) > 256 * 1024:
                            raise NativeWindowError("native helper output limit")
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            row = json.loads(line)
                            last_message = time.monotonic()
                            if row["type"] == "error":
                                raise NativeWindowError(row.get("code", "native observation failed"))
                            if row["type"] == "stopped":
                                stopped = True
                                return
                            if row["type"] == "ready":
                                WindowGeometry.from_snapshot(row["snapshot"])
                                if abs(row["snapshot"]["uptime"] - time.monotonic()) > 2:
                                    raise NativeWindowError("native event clock mismatch")
                                self.snapshot = row["snapshot"]
                            elif row["type"] == "gesture":
                                pointer_intent(row)
                                if self.snapshot is None or any(row["snapshot"][k] != self.snapshot[k] for k in ("pid", "window_id", "config")):
                                    raise NativeWindowError("native recording window changed")
                                down = row["down"]["uptime"]
                                before = next((f for f in reversed(frames) if last_up < f.captured_at <= down), None)
                                if before is None:
                                    raise NativeWindowError("no completed pre-input frame")
                                self.commit(row, before)
                                last_up = row["up"]["uptime"]
                    timeout = self.ready_timeout_s if self.snapshot is None else self.heartbeat_timeout_s
                    if time.monotonic() - last_message > timeout:
                        phase = "ready" if self.snapshot is None else "heartbeat"
                        raise NativeWindowError(f"native helper {phase} timeout")
                    if self.snapshot is not None and time.monotonic() - last_capture >= .12:
                        frame = self.session.capture_preview()
                        if (frame.width, frame.height) != EXPECTED_PREVIEW:
                            raise NativeWindowError("native frame geometry changed")
                        frames.append(frame)
                        last_capture = time.monotonic()
                        self._ready.set()
        except Exception as exc:
            if not self._stop.is_set():
                self.error = f"{type(exc).__name__}: {exc}"
                self._ready.set()
                self.fault(self.error)
        finally:
            self._ready.set()
            self._terminate()

    def _terminate(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                process.stdout.close()

    def freeze(self) -> None:
        """Disable tap, drain complete gestures, then wait for the capture worker."""
        with self._freeze_lock:
            if self._process is not None and self._process.poll() is None:
                self._process.send_signal(signal.SIGUSR1)
            if self._worker is not None:
                self._worker.join(timeout=15)
                if self._worker.is_alive():
                    self.stop()
                    raise NativeWindowError("native recorder freeze timed out")

    def stop(self) -> None:
        """Cancel path discards pending input; no callback may outlive this call."""
        self._stop.set()
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()
        self._terminate()
