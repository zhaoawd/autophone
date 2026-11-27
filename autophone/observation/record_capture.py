"""record_capture.py — 路径 B 录制采集器（人点/拖/输入 viewer → 发 UDS → trace + dry-run IR）。

设计与范围见 docs/record-replay-design.md §三/§七/§八。M0 把核心钉死：**人操作 → UDS → 可复现
的坐标契约 + dry-run IR**。M3 在同一 schema 上补两种事件，覆盖三种 typed-IR 逃生动作：

  · tap   → tap_xy(px,py)        （M0）
  · type  → type_text(text)      （M3：viewer 文本框，剪贴板写入 UIPasteboard）
  · swipe → swipe_xy(px1..py2,ms)（M3：viewer 拖拽，配 recipe.swipe_xy 白名单 + 校验单测）
  · key   → press_key(name)      （硬件键 home/power/volup/voldown，无坐标，名字过封闭集）

核心（`RecordSession`）与 viewer 分离，这样核心可单测、不被 GUI 卡死：
  · 核心：截图、几何锁、坐标换算、发 tap/type/swipe、存 before/after、写 trace、出 dry-run IR。
  · 驱动核心的三种 surface，喂同一个核心：
      ① `--selftest`  注入假 transport，无 VM/GUI，跑通坐标/事件契约（本机可跑）
      ② `--clicks-file` 喂一串动作（tap/type/swipe），对活 VM 真发（无 GUI，VM 起来即可端到端）
      ③ viewer       tkinter 薄窗口（人点/拖 + 文本框输入），GUI 依赖未就绪会 fail-closed 报清楚

坐标铁律（最易翻车处）：可回放的逃生 action 吃 430×932 preview 坐标、内部 ×3 到 native
（vphone_uds.preview_to_native）。所以 trace 同时存 preview+native，**dry-run IR 只用 preview**，
native 仅留痕/校验、绝不回喂（否则二次放大点偏）。type 无坐标，按文本原样回放。

几何不靠猜：从首帧截图实际像素尺寸推 preview；若 ≠ 430×932 直接 fail-closed，不做"聪明缩放"。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from autophone.device.uds import (
    DEFAULT_SOCK,
    EXPECTED_PREVIEW,
    HARDWARE_KEYS,
    NATIVE_SCALE,
    Sample,
    preview_to_native,
    request,
    save_image,
)

TRACE_SCHEMA = "record-trace.v1"  # v1 adds type/swipe events alongside tap (M3)
MAX_LABEL_CHARS = 60  # 事件名要进提升 prompt 与 recipe step，长了既没用也污染上下文

# transport seam: (msg) -> (response_dict, Sample). Defaults to the real UDS
# request; selftest injects a fake so the core runs with no VM and no GUI.
Transport = Callable[[dict[str, Any]], "tuple[dict[str, Any], Sample]"]
SemanticFactory = Callable[[str, Path, Callable[[int, dict[str, Any]], None]], Any | None]


class CaptureError(RuntimeError):
    """A screenshot/tap round-trip came back not-ok, or the response was unusable."""


class GeometryError(RuntimeError):
    """First frame's preview dims ≠ EXPECTED_PREVIEW. Fail closed — never rescale."""


def decode_viewer_image(image_b64: str) -> Any:
    """Decode a UDS screenshot into a Tk-friendly RGB image."""
    import io
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(image_b64))) as im:
        im.load()
        if im.mode != "RGB":
            im = im.convert("RGB")
        return im


def encode_viewer_png_data(image_b64: str) -> str:
    """Convert UDS JPEG base64 to PNG base64 for Tk's native PhotoImage."""
    import io

    buf = io.BytesIO()
    decode_viewer_image(image_b64).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ensure_tk_library_env(*, base_prefix: str | Path | None = None) -> None:
    """Point Tcl/Tk at the uv Python runtime libraries before creating Tk()."""
    base = Path(base_prefix or sys.base_prefix)
    if "TCL_LIBRARY" not in os.environ:
        tcl = next(base.glob("lib/tcl*/init.tcl"), None)
        if tcl is not None:
            os.environ["TCL_LIBRARY"] = str(tcl.parent)
    if "TK_LIBRARY" not in os.environ:
        tk = next(base.glob("lib/tk*/tk.tcl"), None)
        if tk is not None:
            os.environ["TK_LIBRARY"] = str(tk.parent)


@dataclass
class TapEvent:
    kind: str
    preview: list[int]            # [px, py] — replay source, fed to tap_xy
    native: list[int]            # [nx, ny] — provenance/check only, NEVER re-fed
    before_shot: str             # filename of the frame at the moment of the tap
    after_shot: str              # filename of the frame just after the tap landed
    uds_result: dict[str, Any]   # {"ok": bool, "error"?: str}
    t: float                     # seconds since session start
    label: str = ""              # optional human hint (M1 lifter may use it)


@dataclass
class TypeEvent:
    """A clipboard text write (UDS `type` = UIPasteboard setString, not keystrokes —
    CLAUDE.md). No coordinate: replay is the literal text via type_text. The caller
    must have focused a field first (same precondition as CaseContext.type_text)."""
    kind: str
    text: str                    # replay source, fed verbatim to type_text
    before_shot: str
    after_shot: str
    uds_result: dict[str, Any]
    t: float


@dataclass
class SwipeEvent:
    """A drag gesture. Both endpoints are preview coords (replay source, fed to
    swipe_xy); native is derived per-endpoint for provenance only, NEVER re-fed."""
    kind: str
    from_preview: list[int]      # [px1, py1] — replay source
    to_preview: list[int]        # [px2, py2] — replay source
    from_native: list[int]       # [nx1, ny1] — provenance/check only
    to_native: list[int]         # [nx2, ny2] — provenance/check only
    ms: int                      # gesture duration
    before_shot: str
    after_shot: str
    uds_result: dict[str, Any]
    t: float
    label: str = ""              # optional human hint


@dataclass
class KeyEvent:
    """A hardware key press (home/power/volup/voldown). No coordinate: replay is
    the key name via press_key. The name is validated against HARDWARE_KEYS before
    dispatch — the guest only answers "unknown key", which would otherwise look
    like a device that ignored the press."""
    kind: str
    name: str                    # replay source, fed verbatim to press_key
    before_shot: str
    after_shot: str
    uds_result: dict[str, Any]
    t: float


@dataclass(frozen=True)
class PreviewCapture:
    """Display-only capture returned to a surface adapter; never enters trace."""

    image_b64: str
    width: int
    height: int
    captured_at: float


class RecordSession:
    """Core record logic — no GUI. Drive it with record_tap() from any surface.

    Lifecycle: start() locks geometry off the first real frame, then each
    record_tap() captures before-shot → sends the UDS tap → captures after-shot
    and appends a TapEvent. finalize() writes trace.json; dry_run_ir() emits the
    replayable {steps:[{action:"tap_xy", ...}]} (preview coords only)."""

    def __init__(
        self,
        out_dir: str | Path,
        app: str,
        build: str,
        *,
        transport: Transport | None = None,
        sock_path: str = DEFAULT_SOCK,
        starts_with: str | None = None,
        registry_root: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        semantic_factory: SemanticFactory | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.app = app
        self.build = build
        self.starts_with = starts_with
        self._registry_root = Path(registry_root) if registry_root is not None else None
        self._sock_path = sock_path
        # Live UDS unless a test/fake transport is injected. Tracked as a flag, NOT
        # by `transport is self._real_transport`: bound methods are recreated on every
        # attribute access (`self._real_transport is self._real_transport` is False),
        # so an identity guard would always read "injected" and skip the live reset.
        self._live = transport is None
        self._transport: Transport = transport or self._real_transport
        self._clock = clock
        self._semantic_factory = semantic_factory
        self._semantic: Any | None = None
        self._semantic_finished = False
        self._event_semantics: dict[int, dict[str, Any]] = {}
        self._input_observations: dict[int, dict[str, Any]] = {}
        self._t0: float | None = None
        self._shot_idx = 0
        self._geometry: dict[str, Any] | None = None
        self._start_fp: str | None = None
        self._events: list[TapEvent | TypeEvent | SwipeEvent | KeyEvent] = []

    # ── transport ───────────────────────────────────────────────────────────
    def _real_transport(self, msg: dict[str, Any]) -> tuple[dict[str, Any], Sample]:
        return request(msg, sock_path=self._sock_path)

    def _screenshot(self) -> tuple[dict[str, Any], Sample]:
        resp, sample = self._transport({"t": "screenshot"})
        if not resp.get("ok"):
            raise CaptureError(f"screenshot not ok: {resp.get('error')!r}")
        if not resp.get("image"):
            raise CaptureError("screenshot response has no image")
        return resp, sample

    def _save_shot(self, resp: dict[str, Any]) -> str:
        name = f"shot_{self._shot_idx:03d}.jpg"
        self._shot_idx += 1
        save_image(resp, self.out_dir / name)
        return name

    # ── geometry (fail-closed, never guessed) ────────────────────────────────
    def _lock_geometry(self, sample: Sample) -> dict[str, Any]:
        # The screenshot JPEG comes back at PREVIEW resolution (430×932), not
        # native — verified on device 2026-06-25. tap coords are preview×3
        # (preview_to_native). So the image dims ARE the preview space directly;
        # native is what we derive for the trace's tap-coordinate record.
        if sample.image_w is None or sample.image_h is None:
            raise GeometryError("could not decode screenshot dimensions")
        preview = (sample.image_w, sample.image_h)
        if preview != EXPECTED_PREVIEW:
            raise GeometryError(
                f"screenshot dims {preview} != expected preview {EXPECTED_PREVIEW} "
                f"— refusing to rescale (would mis-map every tap). Fail closed.")
        return {
            "preview": list(EXPECTED_PREVIEW),
            "native": [sample.image_w * NATIVE_SCALE, sample.image_h * NATIVE_SCALE],
            "native_scale": NATIVE_SCALE,
        }

    # ── public API ───────────────────────────────────────────────────────────
    def _run_starts_with(self) -> None:
        """Actually drive the device into the declared starting state before the
        first frame is captured, so the recorded taps are taken from the same
        screen `starts_with` claims (and the lifted op's `operation <starts_with>`
        step #1 matches reality). Fail-closed: if the reset can't reach home we
        raise rather than let capture begin on the wrong page.

        Only runs with the live UDS transport — an injected (test/fake) transport
        leaves this a no-op so unit/self-tests don't trigger real dispatch."""
        if not self.starts_with:
            return
        if not self._live:  # injected (test/fake) transport — no real device to reset
            return
        # Lazy: record_capture is a phase1 tool; pull the phase2 engine + per-app
        # operation registry only when a real reset is actually requested.
        from autophone.execution.app_recipes import import_app_recipes
        from autophone.paths import CODE_ROOT
        from autophone.execution.engine.actions import CaseContext
        from autophone.execution.engine.recipe import dispatch_step, operation_scope

        registry_root = self._registry_root or CODE_ROOT
        if not import_app_recipes(self.app, root=registry_root):
            raise CaptureError(
                f"app {self.app!r} has no recipe package; cannot run "
                f"starts_with={self.starts_with!r} — refusing to record on an "
                f"unknown starting state")
        ctx = CaseContext(
            case_id=f"rec-reset-{self.starts_with}",
            shot_dir=self.out_dir / "_reset",
            sock_path=self._sock_path,
        )
        try:
            with operation_scope(self.app, root=registry_root):
                dispatch_step(ctx, {"operation": self.starts_with})
        except Exception as e:  # noqa: BLE001 — any reset failure aborts the record
            raise CaptureError(
                f"starts_with={self.starts_with!r} did not complete ({e}) — "
                f"refusing to start recording on an unverified starting state"
            ) from e

    def start(self) -> dict[str, Any]:
        """Drive into the declared `starts_with` state, then take the first
        frame, lock geometry, fingerprint the start screen. Raises GeometryError
        before any tap if dims aren't the expected preview; raises CaptureError
        if the starts_with reset didn't complete."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._run_starts_with()
        resp, sample = self._screenshot()
        self._geometry = self._lock_geometry(sample)
        raw = base64.b64decode(resp["image"])
        self._start_fp = hashlib.sha256(raw).hexdigest()[:16]
        self._save_shot(resp)  # shot_000 = start frame (the fingerprint source)
        self._t0 = self._clock()
        self._start_semantics()
        return self._geometry

    def _start_semantics(self) -> None:
        if not self._live:
            return
        try:
            factory = self._semantic_factory
            if factory is None:
                from autophone.observation.record_semantics import build_record_semantics
                factory = build_record_semantics
            semantic = factory(self.app, self.out_dir, self._set_event_semantic)
            if semantic is not None:
                self._semantic = semantic
                semantic.start()
        except Exception:
            # Product identity is an advisory stability enhancement.  The screen
            # recording remains useful and liftable when Frida is unavailable.
            self._semantic = None

    def _set_event_semantic(self, index: int, semantic: dict[str, Any]) -> None:
        if 0 <= int(index) < len(self._events):
            self._event_semantics[int(index)] = dict(semantic)

    def _before_event(self) -> int:
        index = len(self._events)
        if self._semantic is not None:
            try:
                self._semantic.before_event(index)
            except Exception:
                pass
        return index

    def _after_event(self, index: int) -> None:
        if self._semantic is not None:
            try:
                self._semantic.after_event(index)
            except Exception:
                pass

    def capture_preview(self) -> PreviewCapture:
        """Capture a frame without saving a shot or mutating the trace."""
        resp, sample = self._screenshot()
        if sample.image_w is None or sample.image_h is None:
            raise CaptureError("could not decode preview dimensions")
        return PreviewCapture(
            image_b64=str(resp["image"]),
            width=sample.image_w,
            height=sample.image_h,
            captured_at=self._clock(),
        )

    def record_tap(self, px: int, py: int, *, label: str = "") -> TapEvent:
        """Record one tap at preview coord (px, py): before-shot → UDS tap →
        after-shot. Coordinates are preview (430×932); native is derived for the
        trace only. Raises if start() hasn't run."""
        if self._t0 is None:
            raise RuntimeError("call start() before record_tap()")
        if not (0 <= px < EXPECTED_PREVIEW[0] and 0 <= py < EXPECTED_PREVIEW[1]):
            raise ValueError(f"preview coord ({px},{py}) outside {EXPECTED_PREVIEW}")
        event_index = self._before_event()
        before_resp, _ = self._screenshot()
        before = self._save_shot(before_resp)
        nx, ny = preview_to_native(px, py)
        tap_resp, _ = self._transport({"t": "tap", "x": nx, "y": ny})
        # Fail closed: a not-ok tap never happened on device — recording it would
        # plant a phantom tap_xy that M1/M2 then lift/promote off a fake event.
        if not tap_resp.get("ok"):
            raise CaptureError(
                f"tap at preview({px},{py}) not ok: {tap_resp.get('error')!r} "
                f"— refusing to record a tap that didn't land")
        after_resp, _ = self._screenshot()
        after = self._save_shot(after_resp)
        ev = TapEvent(
            kind="tap",
            preview=[px, py],
            native=[nx, ny],
            before_shot=before,
            after_shot=after,
            uds_result={"ok": True},  # not-ok already raised above; recorded taps landed
            t=round(self._clock() - self._t0, 3),
            label=label,
        )
        self._events.append(ev)
        self._after_event(event_index)
        return ev

    def record_observed_pointer(self, observation: dict[str, Any], before: PreviewCapture) -> TapEvent | SwipeEvent:
        """Append observed input without dispatching any device action.

        The before frame completed before mouseDown. Delivery remains unverified;
        the native window, timestamps and sampled frame age are retained in trace.
        """
        from autophone.record.native import pointer_intent

        if self._t0 is None:
            raise RuntimeError("call start() before record_observed_pointer()")
        intent = pointer_intent(observation)
        down_at = observation["down"]["uptime"]
        if not 0 <= down_at - before.captured_at <= 1:
            raise CaptureError("no fresh completed frame before native pointer down")
        if (before.width, before.height) != EXPECTED_PREVIEW:
            raise GeometryError("native before frame geometry changed")
        before_name = self._save_shot({"image": before.image_b64})
        after_resp, sample = self._screenshot()
        self._lock_geometry(sample)
        after_name = self._save_shot(after_resp)
        common = dict(before_shot=before_name, after_shot=after_name,
                      uds_result={"dispatched": False}, t=round(down_at - self._t0, 3))
        if intent["kind"] == "tap":
            point = intent["preview"]
            event = TapEvent(kind="tap", preview=point, native=list(preview_to_native(*point)), **common)
        else:
            start, end = intent["from_preview"], intent["to_preview"]
            event = SwipeEvent(kind="swipe", from_preview=start, to_preview=end,
                from_native=list(preview_to_native(*start)), to_native=list(preview_to_native(*end)),
                ms=intent["ms"], **common)
        self._input_observations[len(self._events)] = {
            **observation, "delivery": "unverified", "before_capture_uptime": before.captured_at,
            "before_frame_age_s": down_at - before.captured_at,
        }
        self._events.append(event)
        return event

    def record_type(self, text: str) -> TypeEvent:
        """Record a clipboard text write: before-shot → UDS type → after-shot.
        `text` is replayed verbatim by type_text. Fail closed on a not-ok type
        (recording it would plant a phantom type_text the lifter then promotes off
        a write that never landed). Empty text is refused (nothing to replay)."""
        if self._t0 is None:
            raise RuntimeError("call start() before record_type()")
        if not text:
            raise ValueError("refusing to record an empty type (nothing to replay)")
        event_index = self._before_event()
        before_resp, _ = self._screenshot()
        before = self._save_shot(before_resp)
        # `screen: False` skips the device-side screenshot inside type (CLAUDE.md);
        # we already framed before/after ourselves, so it just stages the clipboard.
        type_resp, _ = self._transport({"t": "type", "text": text, "screen": False})
        if not type_resp.get("ok"):
            raise CaptureError(
                f"type {text!r} not ok: {type_resp.get('error')!r} "
                f"— refusing to record a write that didn't land")
        after_resp, _ = self._screenshot()
        after = self._save_shot(after_resp)
        ev = TypeEvent(
            kind="type", text=text, before_shot=before, after_shot=after,
            uds_result={"ok": True}, t=round(self._clock() - self._t0, 3))
        self._events.append(ev)
        self._after_event(event_index)
        return ev

    def record_key(self, name: str) -> KeyEvent:
        """Record one hardware key press: before-shot → UDS key → after-shot.
        `name` is replayed verbatim by press_key. Fail closed on an unknown name
        (before touching the device) and on a not-ok press (recording it would
        plant a phantom press_key the lifter then promotes off a press that never
        landed)."""
        if self._t0 is None:
            raise RuntimeError("call start() before record_key()")
        if name not in HARDWARE_KEYS:
            raise ValueError(
                f"unknown key {name!r} — expected one of {'/'.join(HARDWARE_KEYS)}")
        event_index = self._before_event()
        before_resp, _ = self._screenshot()
        before = self._save_shot(before_resp)
        # `screen: False` skips the device-side screenshot (CLAUDE.md); we frame
        # before/after ourselves.
        key_resp, _ = self._transport({"t": "key", "name": name, "screen": False})
        if not key_resp.get("ok"):
            raise CaptureError(
                f"key {name!r} not ok: {key_resp.get('error')!r} "
                f"— refusing to record a press that didn't land")
        after_resp, _ = self._screenshot()
        after = self._save_shot(after_resp)
        ev = KeyEvent(
            kind="key", name=name, before_shot=before, after_shot=after,
            uds_result={"ok": True}, t=round(self._clock() - self._t0, 3))
        self._events.append(ev)
        self._after_event(event_index)
        return ev

    def record_swipe(self, px1: int, py1: int, px2: int, py2: int, *,
                     ms: int = 300, label: str = "") -> SwipeEvent:
        """Record a drag from preview (px1,py1) to (px2,py2) over `ms`: before-shot
        → UDS swipe → after-shot. Endpoints are preview (430×932); native derived
        for the trace only. Fail closed on out-of-range coords or a not-ok swipe."""
        if self._t0 is None:
            raise RuntimeError("call start() before record_swipe()")
        for (px, py) in ((px1, py1), (px2, py2)):
            if not (0 <= px < EXPECTED_PREVIEW[0] and 0 <= py < EXPECTED_PREVIEW[1]):
                raise ValueError(f"preview coord ({px},{py}) outside {EXPECTED_PREVIEW}")
        if ms <= 0:
            raise ValueError(f"swipe ms must be > 0, got {ms}")
        event_index = self._before_event()
        before_resp, _ = self._screenshot()
        before = self._save_shot(before_resp)
        nx1, ny1 = preview_to_native(px1, py1)
        nx2, ny2 = preview_to_native(px2, py2)
        swipe_resp, _ = self._transport(
            {"t": "swipe", "x1": nx1, "y1": ny1, "x2": nx2, "y2": ny2, "ms": ms})
        if not swipe_resp.get("ok"):
            raise CaptureError(
                f"swipe ({px1},{py1})->({px2},{py2}) not ok: {swipe_resp.get('error')!r} "
                f"— refusing to record a swipe that didn't land")
        after_resp, _ = self._screenshot()
        after = self._save_shot(after_resp)
        ev = SwipeEvent(
            kind="swipe", from_preview=[px1, py1], to_preview=[px2, py2],
            from_native=[nx1, ny1], to_native=[nx2, ny2], ms=ms,
            before_shot=before, after_shot=after, uds_result={"ok": True},
            t=round(self._clock() - self._t0, 3), label=label)
        self._events.append(ev)
        self._after_event(event_index)
        return ev

    def label_event(self, index: int, label: str) -> TapEvent | SwipeEvent:
        """给已录制的第 `index` 个事件命名（`-1` = 最后一个），返回该事件。

        录制时人心里是知道「这一下点的是确认按钮」的，而事后只剩一张截图和一对
        坐标——提升器只能重新看图猜。命名走的是**已落地事件的元数据**，不碰设备、
        不产生新事件：`label` 已经是 trace schema 的一部分（M0 起），提升期喂进
        prompt，判不准回退时也原样带进 `tap_xy/swipe_xy` 的 label。

        只有 tap / swipe 能命名——回放侧只有 `tap_xy`/`swipe_xy` 收 `label` 参数，
        给 type/key 挂个没人读的名字只会让人以为它进了剧本。空字符串=清掉名字。
        """
        if not self._events:
            raise ValueError("no recorded event to label")
        if not -len(self._events) <= index < len(self._events):
            raise ValueError(
                f"event index {index} out of range (recorded {len(self._events)})")
        event = self._events[index]
        if not isinstance(event, (TapEvent, SwipeEvent)):
            raise ValueError(
                f"{event.kind} 事件不支持 label（只有 tap/swipe 的回放 verb 收 label）")
        text = str(label).strip()
        if len(text) > MAX_LABEL_CHARS:
            raise ValueError(f"label too long (>{MAX_LABEL_CHARS} chars)")
        event.label = text
        return event

    def trace(self) -> dict[str, Any]:
        if self._geometry is None:
            raise RuntimeError("call start() before trace()")
        events = [asdict(e) for e in self._events]
        for index, semantic in self._event_semantics.items():
            if 0 <= index < len(events):
                events[index]["semantic"] = semantic
        for index, observation in self._input_observations.items():
            events[index]["input_observation"] = observation
        trace = {
            "schema": TRACE_SCHEMA,
            "app": self.app,
            "build": self.build,
            "start": {"screen_fp": self._start_fp, "starts_with": self.starts_with},
            "geometry": self._geometry,
            "events": events,
        }
        if self._semantic is not None:
            trace["semantic_capture"] = dict(getattr(self._semantic, "status", {}) or {})
        return trace

    def finish_semantics(self) -> None:
        if self._semantic_finished:
            return
        self._semantic_finished = True
        if self._semantic is not None:
            try:
                self._semantic.finish()
            except Exception:
                self.close_semantics()

    def close_semantics(self) -> None:
        if self._semantic is not None:
            try:
                self._semantic.close()
            except Exception:
                pass

    def finalize(self) -> Path:
        self.finish_semantics()
        path = self.out_dir / "trace.json"
        path.write_text(json.dumps(self.trace(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def dry_run_ir(self) -> dict[str, Any]:
        """Emit the replayable recipe IR. Each event → a flat whitelisted step
        using PREVIEW coords only (tap→tap_xy, type→type_text, swipe→swipe_xy,
        key→press_key) —
        matches tools/phase2/engine/recipe.py step shape, so it can be fed straight
        to validate_recipe. native coords never leak into a step."""
        if not self._events:
            raise CaptureError(
                "no events recorded — refusing to emit an empty, non-replayable IR")
        steps: list[dict[str, Any]] = []
        for e in self._events:
            if isinstance(e, TapEvent):
                step: dict[str, Any] = {"action": "tap_xy", "px": e.preview[0], "py": e.preview[1]}
                if e.label:
                    step["label"] = e.label
            elif isinstance(e, TypeEvent):
                step = {"action": "type_text", "text": e.text}
            elif isinstance(e, SwipeEvent):
                step = {"action": "swipe_xy",
                        "px1": e.from_preview[0], "py1": e.from_preview[1],
                        "px2": e.to_preview[0], "py2": e.to_preview[1], "ms": e.ms}
                if e.label:
                    step["label"] = e.label
            elif isinstance(e, KeyEvent):
                step = {"action": "press_key", "name": e.name}
            else:  # unreachable: only the four recorded event types exist
                raise CaptureError(f"unknown event type {type(e).__name__}")
            steps.append(step)
        return {"steps": steps}


# ── selftest: no VM, no GUI — proves the coordinate/trace contract ───────────
def _fake_transport(*, frame_dims: tuple[int, int] = (430, 932),
                    tap_ok: bool = True, type_ok: bool = True,
                    swipe_ok: bool = True, key_ok: bool = True) -> Transport:
    """A transport that fakes screenshots of a chosen size and tap/type/swipe/key acks.
    Real screenshots come back at PREVIEW size (430×932), so the default matches
    that. The 'image' payload is a tiny stub — save_image only base64-decodes +
    writes, and geometry is read off the Sample dims we set here (not by re-parsing).
    `*_ok=False` fakes a rejected action to exercise that fail-closed path."""
    w, h = frame_dims
    stub = base64.b64encode(b"\xff\xd8\xff\xe0stub-jpeg").decode()
    acks = {"tap": tap_ok, "type": type_ok, "swipe": swipe_ok, "key": key_ok}

    def _t(msg: dict[str, Any]) -> tuple[dict[str, Any], Sample]:
        cmd = msg.get("t")
        if cmd == "screenshot":
            s = Sample(cmd="screenshot", ok=True, rt_ms=1.0, t_write_ms=0.0,
                       t_wait_first_byte_ms=0.0, t_read_total_ms=0.0, bytes_recv=len(stub),
                       image_bytes=9, image_w=w, image_h=h, image_fmt="jpeg")
            return {"ok": True, "image": stub}, s
        if cmd in acks:
            ok = acks[cmd]
            s = Sample(cmd=str(cmd), ok=ok, rt_ms=1.0, t_write_ms=0.0,
                       t_wait_first_byte_ms=0.0, t_read_total_ms=0.0, bytes_recv=0)
            return ({"ok": True} if ok else {"ok": False, "error": f"{cmd} rejected"}), s
        return {"ok": False, "error": f"unexpected msg {msg!r}"}, Sample(
            cmd=str(cmd), ok=False, rt_ms=0.0, t_write_ms=0.0,
            t_wait_first_byte_ms=0.0, t_read_total_ms=0.0, bytes_recv=0)

    return _t


def _run_selftest(tmp: Path) -> int:
    import tempfile

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")

    with tempfile.TemporaryDirectory(dir=tmp) as d:
        # clock that advances 1.0s per call so timestamps are deterministic
        ticks = iter(float(i) for i in range(1000))
        sess = RecordSession(d, "food_ordering", "002", transport=_fake_transport(),
                             starts_with="reset_to_home", clock=lambda: next(ticks))
        geom = sess.start()
        check(geom["preview"] == [430, 932], "geometry preview locked to [430,932] from frame dims")
        check(geom["native"] == [1290, 2796], "geometry native derived ×3 from preview frame dims")

        sess.record_tap(215, 617, label="确认")
        sess.record_tap(40, 880)
        sess.record_type("13800138000")            # M3: clipboard write
        sess.record_swipe(215, 800, 215, 300, ms=400, label="上滑")  # M3: drag
        sess.record_key("home")                    # hardware key (no coord)
        tr = sess.trace()
        ev0 = tr["events"][0]
        check(ev0["preview"] == [215, 617] and ev0["native"] == [645, 1851],
              "event stores both preview and native (645,1851 = 215,617 ×3)")
        check(all(k in ev0 for k in ("before_shot", "after_shot", "uds_result")),
              "event has before_shot / after_shot / uds_result")
        check(ev0["uds_result"]["ok"] is True, "tap uds_result.ok recorded")
        check(tr["start"]["screen_fp"] is not None, "start screen fingerprinted")
        evt = tr["events"][2]
        check(evt["kind"] == "type" and evt["text"] == "13800138000"
              and evt["uds_result"]["ok"] is True, "type event stores text + ok (no coord)")
        evs = tr["events"][3]
        check(evs["kind"] == "swipe" and evs["from_preview"] == [215, 800]
              and evs["to_preview"] == [215, 300] and evs["from_native"] == [645, 2400]
              and evs["ms"] == 400, "swipe event stores from/to preview + native ×3 + ms")
        evk = tr["events"][4]
        check(evk["kind"] == "key" and evk["name"] == "home"
              and evk["uds_result"]["ok"] is True, "key event stores name + ok (no coord)")

        # 事后命名：人手里的语义不该在录制入口丢掉（提升 prompt 与回退 step 都吃它）
        sess.label_event(1, "  底部返回  ")
        check(sess.trace()["events"][1]["label"] == "底部返回", "label_event 命名已录事件（去首尾空白）")
        for bad_index, why in ((2, "type"), (-1, "key")):
            try:
                sess.label_event(bad_index, "x")
                check(False, f"{why} 事件不该能命名")
            except ValueError:
                check(True, f"{why} 事件拒绝命名（回放 verb 不收 label）")
        for bad_arg, why in ((99, "越界索引"), (1, "超长名字")):
            try:
                sess.label_event(bad_arg, "x" * 61 if bad_arg == 1 else "x")
                check(False, f"{why}应被拒")
            except ValueError:
                check(True, f"{why}被拒")

        ir = sess.dry_run_ir()
        s0 = ir["steps"][0]
        check(s0 == {"action": "tap_xy", "px": 215, "py": 617, "label": "确认"},
              "dry-run IR uses preview coords only (no native key)")
        check(ir["steps"][2] == {"action": "type_text", "text": "13800138000"},
              "type → type_text(text) IR step")
        check(ir["steps"][3] == {"action": "swipe_xy", "px1": 215, "py1": 800,
                                 "px2": 215, "py2": 300, "ms": 400, "label": "上滑"},
              "swipe → swipe_xy IR step (preview coords + ms + label)")
        check(ir["steps"][4] == {"action": "press_key", "name": "home"},
              "key → press_key(name) IR step")
        check(ir["steps"][1] == {"action": "tap_xy", "px": 40, "py": 880, "label": "底部返回"},
              "事后命名进得了可回放 IR（回退坐标时人给的语义仍在）")
        check(all("native" not in s and "x" not in s and "x1" not in s for s in ir["steps"]),
              "no native coord leaks into any IR step")

        # the IR must be a real, replayable recipe — validate against the engine
        try:
            from autophone.execution.engine.recipe import validate_recipe
            validate_recipe(ir["steps"])
            check(True, "dry-run IR passes validate_recipe (replayable shape)")
        except ImportError as e:
            print(f"  [SKIP] validate_recipe import unavailable ({e}); IR shape unverified")
        except Exception as e:  # RecipeError etc.
            check(False, f"dry-run IR rejected by validate_recipe: {e}")

        path = sess.finalize()
        check(path.exists() and json.loads(path.read_text(encoding="utf-8"))["schema"] == TRACE_SCHEMA,
              "trace.json written with schema marker")

    # fail-closed: wrong geometry must raise before any tap, no smart rescale
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        bad = RecordSession(d, "food_ordering", "002",
                            transport=_fake_transport(frame_dims=(390, 844)))
        try:
            bad.start()
            check(False, "non-430x932 geometry should fail closed")
        except GeometryError:
            check(True, "non-430x932 geometry fails closed (no rescale)")

    # fail-closed: a not-ok UDS tap must NOT be recorded as a (phantom) replay step
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        nope = RecordSession(d, "food_ordering", "002", transport=_fake_transport(tap_ok=False))
        nope.start()
        try:
            nope.record_tap(100, 100)
            check(False, "not-ok tap should fail closed")
        except CaptureError:
            check(True, "not-ok UDS tap fails closed (CaptureError)")
        check(len(nope._events) == 0, "failed tap leaves no event in the trace")
        try:
            nope.dry_run_ir()
            check(False, "zero-event dry-run IR should fail closed")
        except CaptureError:
            check(True, "zero-event dry-run IR fails closed (refuses empty recipe)")

    # fail-closed: a not-ok UDS type must NOT be recorded as a phantom type_text
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        nt = RecordSession(d, "food_ordering", "002", transport=_fake_transport(type_ok=False))
        nt.start()
        try:
            nt.record_type("hello")
            check(False, "not-ok type should fail closed")
        except CaptureError:
            check(True, "not-ok UDS type fails closed (CaptureError)")
        check(len(nt._events) == 0, "failed type leaves no event in the trace")
        try:
            nt.record_type("")
            check(False, "empty type should be refused")
        except ValueError:
            check(True, "empty type refused (nothing to replay)")

    # fail-closed: a not-ok UDS swipe, out-of-range coord, and non-positive ms
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        ns = RecordSession(d, "food_ordering", "002", transport=_fake_transport(swipe_ok=False))
        ns.start()
        try:
            ns.record_swipe(215, 800, 215, 300)
            check(False, "not-ok swipe should fail closed")
        except CaptureError:
            check(True, "not-ok UDS swipe fails closed (CaptureError)")
        check(len(ns._events) == 0, "failed swipe leaves no event in the trace")
        ok2 = RecordSession(d, "food_ordering", "002", transport=_fake_transport())
        ok2.start()
        try:
            ok2.record_swipe(215, 800, 999, 300)  # x2 outside 430-wide preview
            check(False, "out-of-range swipe coord should be refused")
        except ValueError:
            check(True, "out-of-range swipe coord refused (no rescale)")
        try:
            ok2.record_swipe(215, 800, 215, 300, ms=0)
            check(False, "non-positive swipe ms should be refused")
        except ValueError:
            check(True, "non-positive swipe ms refused")

    # fail-closed: an unknown key name never reaches the device; a not-ok press
    # never becomes a phantom press_key
    with tempfile.TemporaryDirectory(dir=tmp) as d:
        nk = RecordSession(d, "food_ordering", "002", transport=_fake_transport(key_ok=False))
        nk.start()
        try:
            nk.record_key("home")
            check(False, "not-ok key should fail closed")
        except CaptureError:
            check(True, "not-ok UDS key fails closed (CaptureError)")
        check(len(nk._events) == 0, "failed key leaves no event in the trace")
        ok3 = RecordSession(d, "food_ordering", "002", transport=_fake_transport())
        ok3.start()
        try:
            ok3.record_key("back")  # iOS has no hardware back — not in HARDWARE_KEYS
            check(False, "unknown key name should be refused")
        except ValueError:
            check(True, "unknown key name refused before dispatch")
        check(len(ok3._events) == 0, "refused key leaves no event in the trace")

    print(f"\nselftest: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


# ── clicks-file driver: real VM, no GUI ──────────────────────────────────────
def _run_clicks_file(sess: RecordSession, clicks_path: Path) -> None:
    """Drive recording from a JSON action file — end-to-end against a live VM
    without a GUI. Two entry forms (mixable in one file):
      · list  [px,py] / [px,py,"label"]            → tap (legacy form, unchanged)
      · dict  {"tap":  [px,py,"label"?]}            → tap
              {"type": "text"}                      → type (clipboard write)
              {"swipe":[px1,py1,px2,py2, ms?, "label"?]} → swipe (drag)
              {"key":  "home"}                      → hardware key press"""
    actions = json.loads(clicks_path.read_text(encoding="utf-8"))
    sess.start()
    for a in actions:
        if isinstance(a, dict) and "key" in a:
            ev = sess.record_key(str(a["key"]))
            print(f"  key {ev.name} ok={ev.uds_result['ok']} t={ev.t}s")
        elif isinstance(a, dict) and "type" in a:
            ev = sess.record_type(str(a["type"]))
            print(f"  type {a['type']!r} ok={ev.uds_result['ok']} t={ev.t}s")
        elif isinstance(a, dict) and "swipe" in a:
            s = a["swipe"]
            ms = int(s[4]) if len(s) > 4 else 300
            label = str(s[5]) if len(s) > 5 else ""
            ev = sess.record_swipe(int(s[0]), int(s[1]), int(s[2]), int(s[3]),
                                   ms=ms, label=label)
            print(f"  swipe preview{tuple(ev.from_preview)}->{tuple(ev.to_preview)} "
                  f"{ev.ms}ms ok={ev.uds_result['ok']} t={ev.t}s")
        else:
            c = a["tap"] if isinstance(a, dict) else a
            px, py = int(c[0]), int(c[1])
            label = str(c[2]) if len(c) > 2 else ""
            ev = sess.record_tap(px, py, label=label)
            print(f"  tap preview({px},{py}) -> native{tuple(ev.native)} "
                  f"ok={ev.uds_result['ok']} t={ev.t}s")


# ── viewer: tkinter thin window (human clicks). Needs python-tk + Pillow. ─────
def _run_viewer(sess: RecordSession, *, max_events: int | None, duration: float | None,
                fps: float) -> None:
    ensure_tk_library_env()
    try:
        import tkinter as tk
    except Exception as e:  # ImportError or _tkinter missing
        raise SystemExit(
            "viewer needs tkinter + Pillow "
            f"({type(e).__name__}: {e}).\n"
            "On the project venv: `brew install python-tk@3.13` (the brew python "
            "this venv uses ships without _tkinter), Pillow is already a dep.\n"
            "Headless alternatives that need neither: --selftest (no VM) or "
            "--clicks-file <json> (live VM).")

    pw, ph = EXPECTED_PREVIEW  # 430×932; canvas is 1:1 so click x,y == preview coords
    geom = sess.start()        # locks geometry fail-closed BEFORE the window opens
    interval_ms = max(1, int(1000 / max(0.5, fps)))
    t_start = time.monotonic()

    # A drag shorter than this (preview px) is a tap; longer is a swipe. Keeps a
    # slightly-jittered finger-press from being misrecorded as a swipe.
    swipe_threshold = 20

    root = tk.Tk()
    root.title(f"record_capture · {sess.app}/{sess.build} · 点/拖/输入录制")
    root.resizable(False, False)
    canvas = tk.Canvas(root, width=pw, height=ph, highlightthickness=0, bg="black")
    canvas.pack()
    status = tk.Label(root, anchor="w", font=("Menlo", 11))
    status.pack(fill="x")
    # type row: a focused field + send button records a clipboard `type` event.
    type_row = tk.Frame(root)
    type_row.pack(fill="x")
    tk.Label(type_row, text="输入文本:", font=("Menlo", 11)).pack(side="left")
    type_var = tk.StringVar()
    type_entry = tk.Entry(type_row, textvariable=type_var, font=("Menlo", 11))
    type_entry.pack(side="left", fill="x", expand=True)
    img_handle: dict[str, Any] = {}  # keep PhotoImage + canvas item alive across ticks
    press: dict[str, Any] = {}       # drag start, set on <Button-1>

    def _last_desc(e: Any) -> str:
        named = f' "{e.label}"' if getattr(e, "label", "") else ""
        if isinstance(e, TapEvent):
            return f"tap preview{tuple(e.preview)}→native{tuple(e.native)}{named}"
        if isinstance(e, TypeEvent):
            return f"type {e.text!r}"
        if isinstance(e, SwipeEvent):
            return f"swipe preview{tuple(e.from_preview)}→{tuple(e.to_preview)} {e.ms}ms{named}"
        if isinstance(e, KeyEvent):
            return f"key {e.name}"
        return "?"

    def _set_status() -> None:
        n = len(sess._events)
        cap = f"/{max_events}" if max_events else ""
        dur = f" · {int(time.monotonic() - t_start)}s/{int(duration)}s" if duration else ""
        last = sess._events[-1] if sess._events else None
        tail = (f" · last {_last_desc(last)} ok={last.uds_result['ok']}" if last else "")
        status.config(text=f"events {n}{cap}{dur} · geom {geom['preview']}{tail}")

    def _stop() -> None:
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _tick() -> None:
        if duration is not None and time.monotonic() - t_start >= duration:
            return _stop()
        try:
            preview = sess.capture_preview()  # display only — not saved to disk
            photo = tk.PhotoImage(data=encode_viewer_png_data(preview.image_b64), format="png")
            if "item" not in img_handle:
                img_handle["item"] = canvas.create_image(0, 0, anchor="nw", image=photo)
            else:
                canvas.itemconfig(img_handle["item"], image=photo)
            img_handle["ref"] = photo
        except Exception as e:  # keep the loop alive on a transient UDS hiccup
            status.config(text=f"screenshot error: {type(e).__name__}: {e}")
        _set_status()
        root.after(interval_ms, _tick)

    def _clamp(x: int, hi: int) -> int:
        return min(hi - 1, max(0, int(x)))

    def _maybe_stop() -> None:
        if max_events is not None and len(sess._events) >= max_events:
            root.after(150, _stop)

    def _on_press(ev: "tk.Event") -> None:
        press["x"], press["y"], press["t"] = _clamp(ev.x, pw), _clamp(ev.y, ph), time.monotonic()

    def _on_release(ev: "tk.Event") -> None:
        if "x" not in press:
            return
        x0, y0, t0 = press.pop("x"), press.pop("y"), press.pop("t")
        x1, y1 = _clamp(ev.x, pw), _clamp(ev.y, ph)
        is_swipe = (abs(x1 - x0) + abs(y1 - y0)) >= swipe_threshold
        try:
            if is_swipe:
                # gesture duration from the actual press→release time, floored so a
                # flick still gets a usable, replayable ms.
                ms = max(120, int((time.monotonic() - t0) * 1000))
                sess.record_swipe(x0, y0, x1, y1, ms=ms)
                canvas.create_line(x0, y0, x1, y1, fill="#33ddff", width=3, arrow="last")
            else:
                sess.record_tap(x0, y0)
                canvas.create_oval(x0 - 9, y0 - 9, x0 + 9, y0 + 9, outline="#33ff77", width=2)
        except Exception as e:
            status.config(text=f"{'swipe' if is_swipe else 'tap'} error: {type(e).__name__}: {e}")
            return
        _set_status()
        _maybe_stop()

    def _on_type(_ev: "tk.Event | None" = None) -> None:
        text = type_var.get()
        if not text:
            status.config(text="type: 文本为空，未录制")
            return
        try:
            sess.record_type(text)
        except Exception as e:
            status.config(text=f"type error: {type(e).__name__}: {e}")
            return
        type_var.set("")
        _set_status()
        _maybe_stop()

    def _on_label(_ev: "tk.Event | None" = None) -> None:
        # 给刚录的那一步起名：不碰设备，只是把人手里的语义留给提升期。
        try:
            sess.label_event(-1, label_var.get())
        except Exception as e:
            status.config(text=f"label error: {type(e).__name__}: {e}")
            return
        label_var.set("")
        _set_status()

    def _on_key(name: str) -> None:
        try:
            sess.record_key(name)
        except Exception as e:
            status.config(text=f"key error: {type(e).__name__}: {e}")
            return
        _set_status()
        _maybe_stop()

    tk.Button(type_row, text="发送", command=_on_type).pack(side="left")
    # label row: 给最后一个事件命名（只有 tap/swipe 收 label）。
    label_row = tk.Frame(root)
    label_row.pack(fill="x")
    tk.Label(label_row, text="上一步命名:", font=("Menlo", 11)).pack(side="left")
    label_var = tk.StringVar()
    label_entry = tk.Entry(label_row, textvariable=label_var, font=("Menlo", 11))
    label_entry.pack(side="left", fill="x", expand=True)
    tk.Button(label_row, text="命名", command=_on_label).pack(side="left")
    label_entry.bind("<Return>", _on_label)
    # 硬件键行：与浏览器两个 surface 同一套封闭集，桌面录制才不会少一种可录动作。
    key_row = tk.Frame(root)
    key_row.pack(fill="x")
    tk.Label(key_row, text="硬件键:", font=("Menlo", 11)).pack(side="left")
    for key_name in HARDWARE_KEYS:
        tk.Button(key_row, text=key_name,
                  command=lambda n=key_name: _on_key(n)).pack(side="left")
    type_entry.bind("<Return>", _on_type)
    canvas.bind("<Button-1>", _on_press)
    canvas.bind("<ButtonRelease-1>", _on_release)
    root.after(0, _tick)
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="record_capture",
        description="路径 B 录制采集器（人点/拖/输入/硬件键 → UDS tap/swipe/type/key → trace + dry-run IR）")
    ap.add_argument("--out", help="output dir for trace.json + shots (real runs)")
    ap.add_argument("--app", required=True, help="app id for trace header")
    ap.add_argument("--build", default="", help="build label for trace header")
    ap.add_argument("--starts-with", default=None,
                    help="known starting state (e.g. reset_to_home); else null")
    ap.add_argument(
        "--registry-root",
        default=None,
        help="repository root owning apps/<app>/recipes (default: current code tree)",
    )
    ap.add_argument("--max-events", type=int, default=None, help="stop after N events (viewer)")
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (viewer)")
    ap.add_argument("--fps", type=float, default=8.0, help="viewer render cadence")
    ap.add_argument("--clicks-file",
                    help='JSON actions to drive a live VM headless: [px,py] or '
                         '{"tap":[..]}/{"type":"t"}/{"swipe":[px1,py1,px2,py2,ms?,label?]}/{"key":"home"}')
    ap.add_argument("--dry-run-ir", action="store_true",
                    help="after recording, also write/print the dry-run recipe IR")
    ap.add_argument("--selftest", action="store_true",
                    help="no-VM/no-GUI contract check (coordinate + event契约)")
    ap.add_argument("--lease-fd", type=int, default=None,
                    help="inherited ScreenLock fd to adopt (set by the platform launcher)")
    ap.add_argument("--lease-device", default=None, help="device the inherited lease belongs to")
    ap.add_argument("--lease-id", default=None, help="owner id to record for the inherited lease")
    ap.add_argument("--lease-root", default=None,
                    help="ScreenLock root the inherited lease belongs to (default: repo runs/.locks)")
    args = ap.parse_args(argv)

    if args.selftest:
        return _run_selftest(Path("."))

    if not args.out:
        ap.error("--out is required (except for --selftest)")
    lease = _adopt_device_lease(args)
    try:
        sess = RecordSession(args.out, args.app, args.build,
                             starts_with=args.starts_with,
                             registry_root=args.registry_root)

        if args.clicks_file:
            _run_clicks_file(sess, Path(args.clicks_file))
        else:
            _run_viewer(sess, max_events=args.max_events, duration=args.duration, fps=args.fps)

        path = sess.finalize()
        print(f"trace: {path}")
        if args.dry_run_ir:
            ir = sess.dry_run_ir()
            ir_path = Path(args.out) / "dry_run_ir.json"
            ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"dry-run IR: {ir_path}")
            print(json.dumps(ir, ensure_ascii=False, indent=2))
        return 0
    finally:
        # 正常收尾把状态文件写回「没锁着」。被 SIGKILL/SIGTERM 打断时轮不到这里跑，
        # OS 仍会随进程退出释放 flock——留下的陈旧状态由 `live_status` 判活自愈。
        if lease is not None:
            lease.release()


def _adopt_device_lease(args: argparse.Namespace) -> Any:
    """接管父进程 spawn 前拿到、经 `pass_fds` 继承进来的设备锁（桌面录制唯一入口）。

    录制进程**自持真 lease**，而不是靠 `runs/.locks/*.recording.json` 那个 advisory
    holder 文件——sidecar 只是约定，谁忘了看就能和 run / 浏览器录制同时驱动设备。
    没给 `--lease-fd` 时（手动跑 viewer / clicks-file）不持锁，保持原行为。
    """
    if args.lease_fd is None:
        return None
    if not (args.lease_device and args.lease_id):
        raise SystemExit("--lease-fd requires --lease-device and --lease-id")
    from autophone.device.lock import ScreenLock

    # 锁根必须跟发起方同一个：不同 root = 不同 .lock 文件，自检会如实报「没拿到」。
    lock = ScreenLock(root=Path(args.lease_root) if args.lease_root else None)
    lease = lock.adopt_lease(
        args.lease_fd, device=args.lease_device,
        owner_kind="desktop_recording", owner_id=args.lease_id,
    )
    # fail-closed 自检：「以为自己持锁其实没有」比拿不到锁危险得多——后者至少会报错，
    # 前者会让 run / 浏览器录制和这个窗口同时按同一台设备。
    if not lock.verify_held(device=args.lease_device):
        lease.release()
        raise SystemExit(
            f"inherited lease fd {args.lease_fd} does not hold {args.lease_device} — refusing to record")
    return lease


if __name__ == "__main__":
    sys.exit(main())
