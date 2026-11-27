from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageOps

from autophone.contracts import validate_or_raise, write_json
from autophone.assets.screens import screen_by_id
from autophone.util import now_iso, read_json

DEFAULT_GRID = (16, 32)
DEFAULT_THRESHOLD = 0.08


class VisualDiffError(ValueError):
    pass


@dataclass(frozen=True)
class FrameEntry:
    screen_id: str
    file: str
    sha256: str
    source_run_dir: str
    captured_at: str
    volatile_regions: tuple[str, ...] = ()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise VisualDiffError(f"frames manifest not found: {path}")
    validate_or_raise("frames-manifest.v1.json", doc)
    return doc


def frame_by_screen(manifest: dict[str, Any], screen_id: str) -> dict[str, Any] | None:
    for frame in manifest.get("frames") or []:
        if str(frame.get("screen_id") or "") == str(screen_id):
            return frame
    return None


def region_rect(region: Any) -> tuple[float, float, float, float] | None:
    rect = region.get("rect") if isinstance(region, dict) else region
    if not (isinstance(rect, list) and len(rect) == 4):
        return None
    try:
        nx0, ny0, nx1, ny1 = [float(v) for v in rect]
    except (TypeError, ValueError):
        return None
    if 0.0 <= nx0 < nx1 <= 1.0 and 0.0 <= ny0 < ny1 <= 1.0:
        return (nx0, ny0, nx1, ny1)
    return None


def volatile_region_names(screen: dict[str, Any], frame: dict[str, Any]) -> tuple[str, ...]:
    """Regions that must be masked before pixel comparison.

    The historical name is kept for API compatibility.  A sensitive region is
    always masked even when it is visually stable: reports must never depend on
    member codes, balances, account identity, or other PII pixels.
    """
    explicit = tuple(str(v) for v in (frame.get("volatile_regions") or []))
    from_asset = tuple(
        str(name)
        for name, value in (screen.get("regions") or {}).items()
        if isinstance(value, dict)
        and (value.get("volatile") is True or value.get("sensitive") is True)
    )
    return tuple(dict.fromkeys([*explicit, *from_asset]))


def sensitive_region_names(screen: dict[str, Any]) -> tuple[str, ...]:
    """Return screen-owned PII regions that must not leave the local raw run."""
    return tuple(
        str(name)
        for name, value in (screen.get("regions") or {}).items()
        if isinstance(value, dict) and value.get("sensitive") is True
    )


def _open_preview(path: Path, size: tuple[int, int]) -> Image.Image:
    img = Image.open(path).convert("RGB")
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    return img


def _mask_regions(img: Image.Image, screen: dict[str, Any], names: tuple[str, ...]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    draw = ImageDraw.Draw(img)
    regions = screen.get("regions") or {}
    width, height = img.size
    for name in names:
        region = regions.get(name)
        rect = region_rect(region)
        if rect is None:
            warnings.append({
                "code": "dangling_volatile_region",
                "message": f"volatile region {name!r} is not present on screen {screen.get('id')!r}",
                "screen_id": screen.get("id"),
                "region": name,
            })
            continue
        nx0, ny0, nx1, ny1 = rect
        draw.rectangle(
            (int(nx0 * width), int(ny0 * height), int(nx1 * width), int(ny1 * height)),
            fill=(0, 0, 0),
        )
    return warnings


def mask_sensitive_frame(
    source: Path,
    target: Path,
    *,
    screen: dict[str, Any],
) -> dict[str, Any]:
    """Write a shareable frame with every ``sensitive`` region blacked out.

    Raw screenshots remain in the local run directory.  Baseline manifests may
    point only at this derived copy for pages carrying member identity, codes,
    balances, phone numbers, or personalized recommendations.
    """
    source = Path(source)
    target = Path(target)
    if not source.is_file():
        raise VisualDiffError(f"frame not found: {source}")
    image = Image.open(source).convert("RGB")
    names = sensitive_region_names(screen)
    warnings = _mask_regions(image, screen, names)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=92)
    return {
        "ok": True,
        "source": str(source),
        "target": str(target),
        "masked_regions": list(names),
        "warnings": warnings,
    }


def _grid_scores(diff: Image.Image, grid: tuple[int, int]) -> list[dict[str, Any]]:
    cols, rows = grid
    width, height = diff.size
    scores: list[dict[str, Any]] = []
    gray = diff.convert("L")
    for y in range(rows):
        for x in range(cols):
            x0 = int(x * width / cols)
            x1 = int((x + 1) * width / cols)
            y0 = int(y * height / rows)
            y1 = int((y + 1) * height / rows)
            block = gray.crop((x0, y0, x1, y1))
            hist = block.histogram()
            total = sum(hist)
            score = (sum(idx * count for idx, count in enumerate(hist)) / max(1, total)) / 255.0
            scores.append({"x": x, "y": y, "score": round(score, 4)})
    return scores


def compare_frame(
    *,
    screen_id: str,
    screen_asset: dict[str, Any],
    baseline_manifest: dict[str, Any] | None,
    current_manifest: dict[str, Any] | None,
    baseline_dir: Path | None,
    current_dir: Path | None,
    grid: tuple[int, int] = DEFAULT_GRID,
    threshold: float | None = None,
    diff_path: Path | None = None,
) -> dict[str, Any]:
    baseline_frame = frame_by_screen(baseline_manifest or {"frames": []}, screen_id)
    current_frame = frame_by_screen(current_manifest or {"frames": []}, screen_id)
    if baseline_frame is None:
        return {"screen_id": screen_id, "status": "new_screen", "changed_blocks": 0, "warnings": []}
    if current_frame is None:
        return {"screen_id": screen_id, "status": "missing_current_frame", "changed_blocks": 0, "warnings": []}
    if baseline_dir is None or current_dir is None:
        raise VisualDiffError("baseline_dir/current_dir are required when both frames exist")
    baseline_path = baseline_dir / str(baseline_frame["file"])
    current_path = current_dir / str(current_frame["file"])
    if not baseline_path.is_file():
        return {"screen_id": screen_id, "status": "missing_baseline_frame", "changed_blocks": 0, "warnings": []}
    if not current_path.is_file():
        return {"screen_id": screen_id, "status": "missing_current_frame", "changed_blocks": 0, "warnings": []}

    size = tuple(int(v) for v in (current_manifest or baseline_manifest or {}).get("preview_size", [430, 932]))
    if len(size) != 2:
        size = (430, 932)
    limit = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    screen = screen_by_id(screen_asset, screen_id)
    baseline_img = _open_preview(baseline_path, size)
    current_img = _open_preview(current_path, size)
    volatile = volatile_region_names(screen, baseline_frame) + tuple(
        name for name in volatile_region_names(screen, current_frame)
        if name not in volatile_region_names(screen, baseline_frame)
    )
    warnings = _mask_regions(baseline_img, screen, volatile)
    warnings.extend(_mask_regions(current_img, screen, volatile))
    diff = ImageChops.difference(baseline_img, current_img)
    if diff_path is not None:
        diff_path = Path(diff_path)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        ImageOps.colorize(diff.convert("L"), black=(0, 0, 0), white=(255, 48, 0)).save(
            diff_path
        )
    scores = _grid_scores(diff, grid)
    changed = [cell for cell in scores if cell["score"] >= limit]
    return {
        "screen_id": screen_id,
        "status": "changed" if changed else "no_change",
        "threshold": limit,
        "grid": {"cols": grid[0], "rows": grid[1]},
        "changed_blocks": len(changed),
        "blocks": changed,
        "warnings": warnings,
        **({"diff_file": str(diff_path)} if diff_path is not None else {}),
    }


def compare_manifests(
    *,
    screen_asset: dict[str, Any],
    baseline_manifest_path: Path | None,
    current_manifest_path: Path | None,
    grid: tuple[int, int] = DEFAULT_GRID,
) -> list[dict[str, Any]]:
    baseline = load_manifest(baseline_manifest_path) if baseline_manifest_path and baseline_manifest_path.exists() else None
    current = load_manifest(current_manifest_path) if current_manifest_path and current_manifest_path.exists() else None
    baseline_dir = baseline_manifest_path.parent if baseline_manifest_path else None
    current_dir = current_manifest_path.parent if current_manifest_path else None
    screen_ids = {
        str(frame.get("screen_id"))
        for frame in (baseline or {}).get("frames", []) + (current or {}).get("frames", [])
        if frame.get("screen_id")
    }
    threshold = (current or baseline or {}).get("threshold", DEFAULT_THRESHOLD)
    return [
        compare_frame(
            screen_id=screen_id,
            screen_asset=screen_asset,
            baseline_manifest=baseline,
            current_manifest=current,
            baseline_dir=baseline_dir,
            current_dir=current_dir,
            grid=grid,
            threshold=float(threshold),
        )
        for screen_id in sorted(screen_ids)
    ]


def confirm_baseline(
    manifest_path: Path,
    *,
    app: str,
    build: str,
    screen_id: str,
    frame_path: Path,
    source_run_dir: str,
    volatile_regions: list[str] | None = None,
    preview_size: tuple[int, int] = (430, 932),
    evidence_run_dir: Path | None = None,
) -> dict[str, Any]:
    if not frame_path.is_file():
        raise VisualDiffError(f"frame not found: {frame_path}")
    comparison_source = None
    if evidence_run_dir is not None:
        from autophone.observation.impact.comparison import freeze_baseline_source
        comparison_source = freeze_baseline_source(
            run_dir=evidence_run_dir, app=app, build=build, screen_id=screen_id,
            frame_sha256=sha256_file(frame_path),
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    target = manifest_path.parent / f"{screen_id}{frame_path.suffix or '.jpg'}"
    if frame_path.resolve() != target.resolve():
        shutil.copy2(frame_path, target)
    entry = {
        "screen_id": screen_id,
        "file": target.name,
        "sha256": sha256_file(target),
        "source_run_dir": source_run_dir,
        "captured_at": now_iso(),
        "volatile_regions": list(volatile_regions or []),
    }
    if comparison_source is not None:
        entry["comparison_source"] = comparison_source
    manifest = read_json(manifest_path) or {
        "schema_version": "v1",
        "app": app,
        "build": build,
        "preview_size": list(preview_size),
        "frames": [],
    }
    manifest["schema_version"] = "v1"
    manifest["app"] = app
    manifest["build"] = build
    manifest.setdefault("preview_size", list(preview_size))
    frames = [f for f in manifest.get("frames", []) if str(f.get("screen_id") or "") != screen_id]
    frames.append(entry)
    manifest["frames"] = sorted(frames, key=lambda f: str(f.get("screen_id") or ""))
    validate_or_raise("frames-manifest.v1.json", manifest)
    write_json(manifest_path, manifest)
    return {"ok": True, "manifest": str(manifest_path), "frame": entry}
