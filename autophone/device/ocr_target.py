"""ocr_target.py — semantic targeting by text: find "发送" on a screenshot and
tap it, instead of a calibrated pixel coordinate.

Host-side OCR spike (docs/phase1b-semantic-targeting.md). Wraps the Swift
`ocr_locate` (Vision text recognition) and maps recognized boxes from image
pixels to the socket's native tap space, dimension-agnostically:

    native = center_in_image * (NATIVE_DIM / image_dim)

For a real 430×932 screenshot that's the established ×3; for a 2× or otherwise
scaled image it still lands right — so we never assume a fixed screenshot size.

Subcommands:
  dump   <image>                 print all recognized text + boxes
  locate <image> --query 发送     print best match + native tap coords
  probe                          build-free self-test: make a fixture, OCR it,
                                 assert the labels are found and land in the
                                 expected regions (no VM, runs today)
  tap    --query 发送 [--sock …]  screenshot the device, locate, tap (needs VM)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Native tap space (architecture-v1.md §10.3); used as the mapping target.
NATIVE_W, NATIVE_H = 1290, 2796
OCR_DIR = Path(__file__).resolve().parent / "ocr"
def _binary(name: str) -> Path:
    override = os.environ.get("AUTOPHONE_OCR_BIN") if name == "ocr_locate" else None
    if override:
        return Path(override).expanduser().resolve()
    source = OCR_DIR / f"{name}.swift"
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / "Library/Caches")
    return cache / "autophone" / "vision" / platform.machine() / key / name


def _ensure_binary(name: str) -> Path:
    binary = _binary(name)
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    if name == "ocr_locate" and os.environ.get("AUTOPHONE_OCR_BIN"):
        raise OcrError(f"AUTOPHONE_OCR_BIN is not executable: {binary}")
    if sys.platform != "darwin":
        raise OcrError("Vision OCR requires macOS and Xcode Command Line Tools")
    try:
        binary.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=binary.parent) as tmp:
            output = Path(tmp) / name
            result = subprocess.run(
                ["xcrun", "swiftc", "-O", str(OCR_DIR / f"{name}.swift"), "-o", str(output)],
                capture_output=True, text=True,
            )
            if result.returncode:
                raise OcrError(f"Swift compilation failed (check Xcode Command Line Tools): {result.stderr}")
            output.replace(binary)
    except OSError as exc:
        raise OcrError(f"Cannot prepare {name}: {exc}") from exc
    return binary


def ocr_readiness() -> dict:
    binary = _binary("ocr_locate")
    return {"ready": binary.is_file() and os.access(binary, os.X_OK), "path": str(binary)}


DEFAULT_MIN_CONFIDENCE = 0.5


class OcrError(RuntimeError):
    pass


@dataclass
class Match:
    text: str
    confidence: float
    # center in image pixels
    cx: float
    cy: float
    # center mapped to native tap space
    native_x: int
    native_y: int
    # normalized [0,1] center (handy for region constraints / assertions)
    nx: float
    ny: float


def run_ocr(image_path: str | Path) -> dict[str, Any]:
    """Return {"image_w", "image_h", "boxes": [...]}. Raises if the tool is
    missing or fails."""
    proc = subprocess.run([str(_ensure_binary("ocr_locate")), str(image_path)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise OcrError(f"ocr_locate rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _to_match(box: dict[str, Any], iw: int, ih: int) -> Match:
    cx = box["x"] + box["w"] / 2.0
    cy = box["y"] + box["h"] / 2.0
    return Match(
        text=box["text"],
        confidence=float(box["confidence"]),
        cx=cx, cy=cy,
        native_x=round(cx * NATIVE_W / iw),
        native_y=round(cy * NATIVE_H / ih),
        nx=cx / iw, ny=cy / ih,
    )


def find_text(
    ocr: dict[str, Any],
    query: str,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    region: tuple[float, float, float, float] | None = None,
) -> list[Match]:
    """All matches whose text contains `query`, above confidence, optionally
    constrained to a normalized region (nx0, ny0, nx1, ny1). Sorted best-first:
    exact text matches before containing-substring matches, then confidence desc.
    `region` disambiguates a label that appears more than once (e.g. two "发送")."""
    iw, ih = ocr["image_w"], ocr["image_h"]
    out: list[Match] = []
    for box in ocr["boxes"]:
        if query not in box["text"]:
            continue
        m = _to_match(box, iw, ih)
        if m.confidence < min_confidence:
            continue
        if region and not (region[0] <= m.nx <= region[2]
                           and region[1] <= m.ny <= region[3]):
            continue
        out.append(m)
    out.sort(key=lambda m: (m.text != query, -m.confidence))
    return out


# ── subcommands ──────────────────────────────────────────────────────────

def _cmd_dump(args: argparse.Namespace) -> int:
    ocr = run_ocr(args.image)
    print(f"image {ocr['image_w']}x{ocr['image_h']}, {len(ocr['boxes'])} boxes")
    for b in ocr["boxes"]:
        print(f"  {b['confidence']:.2f}  ({b['x']:.0f},{b['y']:.0f} "
              f"{b['w']:.0f}x{b['h']:.0f})  {b['text']!r}")
    return 0


def _cmd_locate(args: argparse.Namespace) -> int:
    ocr = run_ocr(args.image)
    matches = find_text(ocr, args.query, min_confidence=args.min_confidence)
    if not matches:
        print(f"no match for {args.query!r} (>= conf {args.min_confidence})",
              file=sys.stderr)
        return 1
    m = matches[0]
    print(json.dumps({
        "text": m.text, "confidence": round(m.confidence, 3),
        "center_image": [round(m.cx, 1), round(m.cy, 1)],
        "center_norm": [round(m.nx, 3), round(m.ny, 3)],
        "native_tap": [m.native_x, m.native_y],
        "other_matches": len(matches) - 1,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """Deterministic self-test of the full OCR + Y-flip + mapping pipeline."""
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "fixture.png"
        subprocess.run([str(_ensure_binary("make_fixture")), str(img)], check=True)
        ocr = run_ocr(img)

    # Each label must be found AND land in its expected normalized region.
    # (region check validates the Vision Y-flip end to end.)
    checks = [
        ("群聊名称", lambda m: m.ny < 0.30),                  # top
        ("相册",     lambda m: m.nx < 0.50 and m.ny > 0.50),  # lower-left
        ("发送",     lambda m: m.nx > 0.50 and m.ny > 0.50),  # lower-right
    ]
    ok = True
    print(f"fixture {ocr['image_w']}x{ocr['image_h']}, {len(ocr['boxes'])} boxes")
    for text, region_ok in checks:
        ms = find_text(ocr, text)
        if not ms:
            print(f"  FAIL  {text!r} not recognized"); ok = False; continue
        m = ms[0]
        place_ok = region_ok(m)
        print(f"  {'ok  ' if place_ok else 'FAIL'}  {text!r} "
              f"norm=({m.nx:.2f},{m.ny:.2f}) native=({m.native_x},{m.native_y}) "
              f"conf={m.confidence:.2f}")
        ok = ok and place_ok
    print("PROBE PASS" if ok else "PROBE FAIL")
    return 0 if ok else 1


def _cmd_tap(args: argparse.Namespace) -> int:
    """Live: screenshot -> OCR -> locate -> tap. Needs a reachable vphone.sock."""
    import base64
    from autophone.device.uds import request  # local import: device path only

    obj, _ = request({"t": "screenshot"}, sock_path=args.sock, timeout=args.timeout)
    if not obj.get("image"):
        print("screenshot returned no image", file=sys.stderr)
        return 3
    img = Path(tempfile.mktemp(suffix=".jpg"))
    img.write_bytes(base64.b64decode(obj["image"]))
    try:
        ocr = run_ocr(img)
    finally:
        img.unlink(missing_ok=True)

    region = tuple(args.region) if args.region else None
    matches = find_text(ocr, args.query, min_confidence=args.min_confidence,
                        region=region)
    if not matches:
        print(f"no match for {args.query!r}; nothing tapped", file=sys.stderr)
        return 1
    m = matches[0]
    print(f"tapping {m.text!r} at native ({m.native_x},{m.native_y}) "
          f"conf={m.confidence:.2f}")
    if args.dry_run:
        return 0
    resp, _ = request({"t": "tap", "x": m.native_x, "y": m.native_y},
                      sock_path=args.sock, timeout=args.timeout)
    if not resp.get("ok"):
        print(f"tap failed: {resp.get('error')}", file=sys.stderr)
        return 4
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dump", help="print all recognized text + boxes")
    pd.add_argument("image")
    pd.set_defaults(func=_cmd_dump)

    pl = sub.add_parser("locate", help="best match for --query + native coords")
    pl.add_argument("image")
    pl.add_argument("--query", required=True)
    pl.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    pl.set_defaults(func=_cmd_locate)

    pp = sub.add_parser("probe", help="deterministic self-test (no VM)")
    pp.set_defaults(func=_cmd_probe)

    pt = sub.add_parser("tap", help="screenshot -> locate -> tap (needs VM)")
    pt.add_argument("--query", required=True)
    pt.add_argument("--sock", default=None)
    pt.add_argument("--timeout", type=float, default=15.0)
    pt.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    pt.add_argument("--region", type=float, nargs=4, metavar=("NX0", "NY0", "NX1", "NY1"),
                    help="normalized region to constrain the match")
    pt.add_argument("--dry-run", action="store_true")
    pt.set_defaults(func=_cmd_tap)

    args = ap.parse_args()
    # vphone_uds supplies the default sock only when we actually go to device.
    if getattr(args, "sock", None) is None and args.cmd == "tap":
        from autophone.device.uds import DEFAULT_SOCK
        args.sock = DEFAULT_SOCK
    try:
        return args.func(args)
    except OcrError as e:
        print(f"error: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
