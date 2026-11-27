"""screen_record.py — host-side screen recording for the Phase 1B spike.

Two recorders behind one CLI (see docs/phase1b-screen-recording.md):

  preview   PATH A — works TODAY, zero guest change. Polls the UDS `screenshot`
            command in a loop, saves each JPEG, records the real presentation
            timestamp of every frame, and (if ffmpeg is present) muxes them into
            an .mp4 with a *variable* per-frame duration so playback runs at true
            wall-clock speed. Cadence is bounded by screenshot round-trip (~5–15
            fps, not 30) — this is a preview, not native quality.

  trigger   PATH B — drives an optional externally supplied ReplayKit dylib
            over a caller-supplied guest-shell command template
            (--exec-cmd), so it does not own the guest transport. Fires start,
            waits, fires stop, polls the `.done` marker. The .mp4 stays ON the
            guest; pull it back with the UDS `file_get` command (`save` mode).

  selftest  No VM, no UDS: exercises the manifest + concat-file + mux plumbing on
            synthetic frames, so the path-A muxing logic is verifiable today.

GeometryGuard fails closed: if screenshot geometry drifts mid-record the preview
recorder aborts rather than muxing mixed-size frames into a corrupt file.
"""
from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autophone.device.uds import (  # noqa: E402
    DEFAULT_SOCK, GeometryDrift, GeometryGuard, ProtocolError, request,
)

# Darwin notification names + markers — MUST stay in sync with screen_record.m.
NOTIFY_START = "com.autophone.rec.start"
NOTIFY_STOP = "com.autophone.rec.stop"
LOADED_MARKER = "/tmp/autophone_rec.loaded"
DEFAULT_REC_PATH = "/tmp/autophone_rec.mp4"

# A 1×1 baseline JPEG — used only by selftest to stand in for real frames so the
# manifest/concat/mux plumbing is exercisable with no VM and no UDS.
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
    "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


class RecordError(RuntimeError):
    pass


# ── path A: host preview recorder ───────────────────────────────────────────

@dataclass
class Frame:
    path: Path
    t_rel: float                   # seconds since record start (presentation ts)


@dataclass
class PreviewResult:
    frames: list[Frame]
    out_dir: Path
    geometry: tuple[int, int] | None
    duration_s: float
    mp4: Path | None = None
    ffmpeg_cmd: list[str] = field(default_factory=list)

    @property
    def fps(self) -> float:
        # Measured average over the actually-captured span, not a target.
        if len(self.frames) < 2 or self.duration_s <= 0:
            return 0.0
        return (len(self.frames) - 1) / self.duration_s


def record_preview(
    *,
    duration: float,
    out_dir: str | Path,
    sock: str = DEFAULT_SOCK,
    timeout: float = 15.0,
    max_fps: float | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> PreviewResult:
    """Path A: poll `screenshot` for `duration` seconds, saving each frame.

    max_fps optionally throttles the loop (sleeps to keep cadence below the cap);
    by default it runs as fast as the screenshot round-trip allows.
    """
    if duration <= 0:
        raise RecordError(f"duration must be > 0, got {duration}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    guard = GeometryGuard()
    frames: list[Frame] = []
    min_interval = (1.0 / max_fps) if max_fps and max_fps > 0 else 0.0

    t0 = clock()
    last_capture = None
    while True:
        now = clock()
        elapsed = now - t0
        if elapsed >= duration:
            break
        if min_interval and last_capture is not None:
            wait = min_interval - (now - last_capture)
            if wait > 0:
                time.sleep(wait)
        obj, sample = request({"t": "screenshot"}, sock_path=sock, timeout=timeout)
        if not obj.get("ok") or not obj.get("image"):
            raise RecordError(f"screenshot failed: {obj.get('error')!r}")
        guard.check(sample)        # raises GeometryDrift on dims change
        last_capture = clock()
        t_rel = last_capture - t0
        fp = out / f"frame_{len(frames) + 1:05d}.jpg"
        fp.write_bytes(base64.b64decode(obj["image"]))
        frames.append(Frame(fp, t_rel))

    if not frames:
        raise RecordError("captured zero frames (duration too short for one RT?)")

    span = frames[-1].t_rel - frames[0].t_rel
    return PreviewResult(
        frames=frames,
        out_dir=out,
        geometry=guard.locked,
        duration_s=span,
    )


# ── muxing (variable per-frame duration via the ffmpeg concat demuxer) ───────

def frame_durations(frames: list[Frame], *, tail: float = 0.1) -> list[float]:
    """Per-frame display durations from successive timestamps.

    Frame i shows until frame i+1's timestamp; the final frame has no successor
    so it gets the average of the others (or `tail` if only one frame).
    """
    if not frames:
        return []
    durs = [max(0.001, frames[i + 1].t_rel - frames[i].t_rel)
            for i in range(len(frames) - 1)]
    last = (sum(durs) / len(durs)) if durs else tail
    durs.append(max(0.001, last))
    return durs


def write_concat_file(frames: list[Frame], durations: list[float],
                      list_path: str | Path) -> Path:
    """Write an ffmpeg concat-demuxer script with per-frame durations.

    The concat demuxer ignores the duration of the *last* `file` directive, so we
    repeat the final frame to make its duration take effect (documented quirk).
    """
    if len(frames) != len(durations):
        raise RecordError("frames/durations length mismatch")
    lines = ["ffconcat version 1.0"]
    for fr, d in zip(frames, durations):
        lines.append(f"file '{fr.path.name}'")
        lines.append(f"duration {d:.4f}")
    if frames:
        lines.append(f"file '{frames[-1].path.name}'")   # repeat to apply last dur
    p = Path(list_path)
    p.write_text("\n".join(lines) + "\n")
    return p


def ffmpeg_mux_cmd(list_path: str | Path, out_path: str | Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path), "-vsync", "vfr",
        "-pix_fmt", "yuv420p", str(out_path),
    ]


def mux_preview(result: PreviewResult, *, out_path: str | Path | None = None) -> PreviewResult:
    """Mux a PreviewResult's frames into an mp4 if ffmpeg is available.

    Always writes the concat list (so the user can mux later by hand) and records
    the exact ffmpeg command on the result, whether or not ffmpeg is present.
    """
    durs = frame_durations(result.frames)
    list_path = result.out_dir / "frames.concat"
    write_concat_file(result.frames, durs, list_path)
    out = Path(out_path) if out_path else (result.out_dir / "recording.mp4")
    cmd = ffmpeg_mux_cmd(list_path, out)
    result.ffmpeg_cmd = cmd
    if shutil.which("ffmpeg") is None:
        return result             # plumbing ready; caller can run cmd later
    subprocess.run(cmd, check=True, capture_output=True)
    result.mp4 = out
    return result


# ── path B: drive the guest ReplayKit dylib over a shell template ────────────

def _run_guest(exec_cmd: str, guest_cmd: str, *, timeout: float = 30.0) -> tuple[int, str]:
    """Run one guest command through the caller's --exec-cmd template.

    The template must contain `{cmd}`; we substitute the guest command verbatim
    (the template owns any quoting/escaping for its transport, e.g. ssh).
    """
    if "{cmd}" not in exec_cmd:
        raise RecordError("--exec-cmd must contain a {cmd} placeholder")
    full = exec_cmd.replace("{cmd}", guest_cmd)
    proc = subprocess.run(full, shell=True, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


@dataclass
class TriggerResult:
    rec_path: str
    done_status: str | None        # "ok" | "failed" | None (never appeared)
    elapsed_s: float


def trigger_capture(
    *,
    exec_cmd: str,
    duration: float,
    rec_path: str = DEFAULT_REC_PATH,
    poll_timeout: float = 20.0,
    poll_interval: float = 0.5,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> TriggerResult:
    """Path B: fire start, wait `duration`, fire stop, poll the `.done` marker.

    Leaves the muxed .mp4 + `<rec_path>.done` ON THE GUEST. Pull the file back
    with the UDS `file_get` command (`save` mode).
    """
    if duration <= 0:
        raise RecordError(f"duration must be > 0, got {duration}")
    done = f"{rec_path}.done"
    t0 = clock()

    rc, out = _run_guest(exec_cmd, f"notifyutil -p {NOTIFY_START}")
    if rc != 0:
        raise RecordError(f"start trigger failed (rc={rc}): {out.strip()}")
    sleep(duration)
    rc, out = _run_guest(exec_cmd, f"notifyutil -p {NOTIFY_STOP}")
    if rc != 0:
        raise RecordError(f"stop trigger failed (rc={rc}): {out.strip()}")

    # Poll the done marker: `cat` returns "ok"/"failed" once finishWriting flushes.
    status: str | None = None
    deadline = clock() + poll_timeout
    while clock() < deadline:
        rc, out = _run_guest(exec_cmd, f"cat {done} 2>/dev/null")
        text = out.strip()
        if rc == 0 and text in ("ok", "failed"):
            status = text
            break
        sleep(poll_interval)

    return TriggerResult(rec_path=rec_path, done_status=status,
                         elapsed_s=clock() - t0)


# ── subcommands ──────────────────────────────────────────────────────────────

def _cmd_preview(args: argparse.Namespace) -> int:
    try:
        result = record_preview(
            duration=args.duration, out_dir=args.out, sock=args.sock,
            timeout=args.timeout, max_fps=args.max_fps,
        )
    except GeometryDrift as e:
        print(f"ABORT — geometry drift mid-record: {e}", file=sys.stderr)
        return 3
    if not args.no_mux:
        result = mux_preview(result)
    geo = f"{result.geometry[0]}x{result.geometry[1]}" if result.geometry else "?"
    print(f"captured {len(result.frames)} frames over {result.duration_s:.2f}s "
          f"({result.fps:.1f} fps measured) geometry={geo}")
    print(f"frames + concat list in {result.out_dir}/")
    if result.mp4:
        print(f"muxed → {result.mp4}")
    elif not args.no_mux:
        print("ffmpeg not found — frames.concat written; mux later with:")
        print("  " + " ".join(result.ffmpeg_cmd))
    return 0


def _cmd_trigger(args: argparse.Namespace) -> int:
    try:
        res = trigger_capture(
            exec_cmd=args.exec_cmd, duration=args.duration,
            rec_path=args.rec_path, poll_timeout=args.poll_timeout,
        )
    except subprocess.TimeoutExpired as e:
        print(f"guest command timed out: {e}", file=sys.stderr)
        return 2
    if res.done_status == "ok":
        print(f"capture ok → {res.rec_path} on guest ({res.elapsed_s:.1f}s)")
        print("pull it back with the UDS file_get command, e.g. request(")
        print(f"  {{'t': 'file_get', 'path': '{res.rec_path}', 'save': './recording.mp4'}})")
        return 0
    if res.done_status == "failed":
        print(f"guest reported mux FAILED for {res.rec_path}", file=sys.stderr)
        return 4
    print(f"no .done marker after {args.poll_timeout}s — is the dylib injected "
          f"({LOADED_MARKER}) and ReplayKit available in the VM?", file=sys.stderr)
    return 5


def _cmd_selftest(args: argparse.Namespace) -> int:
    """No VM, no UDS: synthesize frames and exercise the mux plumbing."""
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="screen_record_selftest_"))
    # Fabricate frames with a deliberately *uneven* cadence so the variable
    # per-frame duration logic is actually tested (not a constant fps).
    ts = [0.0, 0.08, 0.21, 0.30, 0.55]
    frames: list[Frame] = []
    for i, t in enumerate(ts):
        fp = work / f"frame_{i + 1:05d}.jpg"
        fp.write_bytes(_TINY_JPEG)
        frames.append(Frame(fp, t))
    result = PreviewResult(frames=frames, out_dir=work, geometry=(1, 1),
                           duration_s=ts[-1] - ts[0])

    durs = frame_durations(frames)
    assert len(durs) == len(frames), "duration per frame"
    # Interior durations equal successive gaps; last is the average of the rest.
    assert abs(durs[0] - 0.08) < 1e-6 and abs(durs[1] - 0.13) < 1e-6, durs
    list_path = write_concat_file(frames, durs, work / "frames.concat")
    body = list_path.read_text()
    assert body.startswith("ffconcat version 1.0"), "concat header"
    # Last frame repeated so its duration applies (concat demuxer quirk).
    assert body.count(f"file '{frames[-1].path.name}'") == 2, "last frame repeated"

    result = mux_preview(result, out_path=work / "out.mp4")
    muxed = result.mp4 is not None and result.mp4.exists()
    print(f"selftest OK: {len(frames)} frames, durations={[round(d,3) for d in durs]}")
    print(f"  concat list: {list_path}")
    if muxed:
        print(f"  ffmpeg present → muxed {result.mp4} "
              f"({result.mp4.stat().st_size} bytes)")
    else:
        print("  ffmpeg absent → mux command recorded (plumbing verified):")
        print("    " + " ".join(result.ffmpeg_cmd))
    print(f"  workdir: {work}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sock", default=DEFAULT_SOCK)
    ap.add_argument("--timeout", type=float, default=15.0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("preview", help="path A: record via UDS screenshot polling")
    pp.add_argument("--duration", type=float, required=True, help="seconds to record")
    pp.add_argument("--out", default="/tmp/screen_record")
    pp.add_argument("--max-fps", type=float, default=None,
                    help="throttle cadence below this cap (default: as fast as RT)")
    pp.add_argument("--no-mux", action="store_true",
                    help="save frames only, skip concat/mux")
    pp.set_defaults(func=_cmd_preview)

    pt = sub.add_parser("trigger", help="path B: drive the injected ReplayKit dylib")
    pt.add_argument("--duration", type=float, required=True, help="seconds to record")
    pt.add_argument("--exec-cmd", required=True,
                    help="guest-shell template containing {cmd}, e.g. 'ssh root@vm {cmd}'")
    pt.add_argument("--rec-path", default=DEFAULT_REC_PATH,
                    help=f"guest output path (default {DEFAULT_REC_PATH})")
    pt.add_argument("--poll-timeout", type=float, default=20.0,
                    help="seconds to wait for the .done marker after stop")
    pt.set_defaults(func=_cmd_trigger)

    ps = sub.add_parser("selftest", help="no-VM check of the mux plumbing")
    ps.set_defaults(func=_cmd_selftest)

    args = ap.parse_args()
    try:
        return args.func(args)
    except (RecordError, ProtocolError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
