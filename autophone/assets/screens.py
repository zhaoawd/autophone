"""Screen skeleton S0 assets and matcher.

S0 is deliberately small: load ``apps/<app>/screens/<build>.yaml`` assets,
validate the ``meta + screens`` list contract, and match OCR text against
``must`` / ``forbid`` / ``any_of(min_hits)``. It does not interpret future
``include`` / ``regions`` / active-tab chrome composition fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from autophone.assets import store as asset_store
from autophone.assets.policy import assert_assets_writable
from autophone.contracts import ContractError, validate_or_raise

DEFAULT_MIN_CONFIDENCE = 0.0
WHERE_REGIONS: dict[str, tuple[float, float, float, float]] = {
    "top": (0.0, 0.0, 1.0, 0.20),
    "bottom": (0.0, 0.80, 1.0, 1.0),
    "left-rail": (0.0, 0.0, 0.30, 1.0),
    "right-rail": (0.70, 0.0, 1.0, 1.0),
    "content": (0.0, 0.15, 1.0, 0.85),
}


class ScreenAssetError(ValueError):
    """Screen asset validation or matching failed."""


class ScreenSignatureConflict(ScreenAssetError):
    """More than one screen signature matched the same OCR frame."""


SCREENS_SPEC = asset_store.AssetSpec(
    list_key="screens",
    schema_version=1,
    editable_scalars=frozenset(),
    id_prefix="S",
)


@dataclass(frozen=True)
class Anchor:
    text: str
    where: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class TokenHit:
    text: str
    where: str | None
    box_text: str
    confidence: float
    center_norm: tuple[float, float]


@dataclass(frozen=True)
class ScreenMatch:
    screen_id: str
    status: str
    matched: bool
    reason: str
    tokens_hit: tuple[str, ...]
    tokens_missed: tuple[str, ...]
    forbid_hit: tuple[str, ...] = ()
    any_of_hits: tuple[str, ...] = ()
    any_of_min_hits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id,
            "status": self.status,
            "matched": self.matched,
            "reason": self.reason,
            "tokens_hit": list(self.tokens_hit),
            "tokens_missed": list(self.tokens_missed),
            "forbid_hit": list(self.forbid_hit),
            "any_of_hits": list(self.any_of_hits),
            "any_of_min_hits": self.any_of_min_hits,
        }


def screen_by_id(doc: dict[str, Any], screen_id: str) -> dict[str, Any]:
    for screen in doc.get("screens") or []:
        if str(screen.get("id")) == screen_id:
            return screen
    raise KeyError(screen_id)


def region_for_point(
    screen: dict[str, Any],
    point: tuple[float, float] | list[float],
    *,
    image_w: float = 430.0,
    image_h: float = 932.0,
) -> tuple[str, list[float]] | None:
    """Return the named region containing a point in image/preview pixels."""
    if image_w <= 0 or image_h <= 0 or len(point) != 2:
        raise ScreenAssetError("region_for_point requires valid image dimensions and a 2D point")
    nx = float(point[0]) / image_w
    ny = float(point[1]) / image_h
    for name, box in (screen.get("regions") or {}).items():
        if not _valid_region_box(box):
            raise ScreenAssetError(f"{screen.get('id')}.{name} region 非法：{box!r}")
        nx0, ny0, nx1, ny1 = _region_rect(box)
        if nx0 <= nx <= nx1 and ny0 <= ny <= ny1:
            return str(name), [nx0, ny0, nx1, ny1]
    return None


def set_screen_status(path: Path, screen_id: str, status: str) -> dict[str, Any]:
    """Update only a screen's lifecycle status via the shared asset store."""
    load_screen_asset(path)
    out = asset_store.update_item(SCREENS_SPEC, path, screen_id, {"status": status})
    load_screen_asset(path)
    return out


def deprecate_screen(path: Path, screen_id: str, *, reason: str | None = None) -> dict[str, Any]:
    """Soft-delete one screen signature without bumping its revision."""
    load_screen_asset(path)
    out = asset_store.deprecate_item(SCREENS_SPEC, path, screen_id, reason=reason)
    load_screen_asset(path)
    return out


def evaluate_screen_promotion(
    telemetry: Iterable[dict[str, Any] | ScreenMatch],
    *,
    screen_id: str,
    review: dict[str, Any] | None = None,
    required_matches: int = 3,
) -> dict[str, Any]:
    """Decide whether a draft screen signature is ready to promote.

    S2 evidence is matching telemetry, not operation execution. A screen is ready
    only after human review confirms the chrome/content split and enough real
    frames matched without must-miss or forbid-conflict evidence.
    """
    if required_matches < 1:
        raise ScreenAssetError("required_matches must be >= 1")
    review = review or {}
    relevant = [_telemetry_dict(row) for row in telemetry if _telemetry_screen_id(row) == screen_id]
    clean = [row for row in relevant if _telemetry_clean_match(row)]
    must_miss = [row for row in relevant if _telemetry_has(row, "tokens_missed")]
    forbid_conflicts = [row for row in relevant if _telemetry_has(row, "forbid_hit")]
    signature_conflicts = [
        row for row in relevant
        if str(row.get("reason") or "") in {"signature_conflict", "conflict"}
    ]

    review_ok = review.get("status") == "approved"
    split_ok = review.get("chrome_content_reviewed") is True
    ready = (
        review_ok
        and split_ok
        and len(clean) >= required_matches
        and not must_miss
        and not forbid_conflicts
        and not signature_conflicts
    )
    gaps: list[str] = []
    if not review_ok:
        gaps.append("human_review_not_approved")
    if not split_ok:
        gaps.append("chrome_content_split_not_reviewed")
    if len(clean) < required_matches:
        gaps.append(f"screen_matches_{len(clean)}_lt_{required_matches}")
    if must_miss:
        gaps.append("must_miss_present")
    if forbid_conflicts:
        gaps.append("forbid_conflict_present")
    if signature_conflicts:
        gaps.append("signature_conflict_present")

    return {
        "screen_id": screen_id,
        "ready": ready,
        "required_matches": required_matches,
        "clean_matches": len(clean),
        "gaps": gaps,
        "review": review,
        "telemetry_count": len(relevant),
    }


def promote_screen_if_ready(
    path: Path,
    screen_id: str,
    telemetry: Iterable[dict[str, Any] | ScreenMatch],
    *,
    review: dict[str, Any] | None = None,
    required_matches: int = 3,
) -> dict[str, Any]:
    """Promote one draft screen to active when S2 evidence is sufficient."""
    assert_assets_writable("promote screen asset", path=path)
    doc = load_screen_asset(path)
    screen = _screen_by_id(doc, screen_id)
    if screen.get("status") != "draft":
        raise ScreenAssetError(f"{screen_id} status={screen.get('status')!r}, only draft can be promoted")
    decision = evaluate_screen_promotion(
        telemetry,
        screen_id=screen_id,
        review=review,
        required_matches=required_matches,
    )
    if not decision["ready"]:
        raise ScreenAssetError(f"screen promotion gate not ready: {decision['gaps']}")
    out = set_screen_status(path, screen_id, "active")
    out["promotion_decision"] = decision
    return out


def load_screen_asset(path: Path) -> dict[str, Any]:
    """Load and validate a S0 screen asset YAML file."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    validate_screen_asset(doc, path=path)
    return doc


def validate_screen_asset(doc: dict[str, Any], *, path: Path | None = None) -> None:
    """Validate schema plus invariants that are clearer in Python."""
    try:
        validate_or_raise("screen-asset.v1.json", doc)
    except ContractError as exc:
        loc = f"{path}: " if path else ""
        raise ScreenAssetError(f"{loc}{exc}") from exc

    seen: set[str] = set()
    for screen in doc.get("screens") or []:
        screen_id = str(screen["id"])
        if screen_id in seen:
            raise ScreenAssetError(f"screen id 重复：{screen_id}")
        seen.add(screen_id)

        any_of = screen.get("any_of") or {"min_hits": 0, "tokens": []}
        min_hits = int(any_of.get("min_hits") or 0)
        tokens = any_of.get("tokens") or []
        if min_hits > len(tokens):
            raise ScreenAssetError(
                f"{screen_id}.any_of.min_hits={min_hits} 大于 tokens 数量 {len(tokens)}"
            )
        for name, box in (screen.get("regions") or {}).items():
            if not _valid_region_box(box):
                raise ScreenAssetError(
                    f"{screen_id}.regions.{name} 必须是归一化 [nx0,ny0,nx1,ny1] "
                    "或 {rect, volatile, sensitive}"
                )


def match_screen(
    ocr: dict[str, Any],
    screen: dict[str, Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> ScreenMatch:
    """Match one screen signature against one OCR frame."""
    screen_id = str(screen["id"])
    status = str(screen.get("status") or "")
    forbid_hits = _hit_texts(ocr, _anchors(screen.get("forbid") or []), min_confidence=min_confidence)
    if forbid_hits:
        return ScreenMatch(
            screen_id=screen_id,
            status=status,
            matched=False,
            reason="forbid_hit",
            tokens_hit=tuple(forbid_hits),
            tokens_missed=(),
            forbid_hit=tuple(forbid_hits),
        )

    must = _anchors(screen.get("must") or [])
    must_hits = _hit_texts(ocr, must, min_confidence=min_confidence)
    must_missed = [a.text for a in must if a.text not in must_hits]
    if must_missed:
        return ScreenMatch(
            screen_id=screen_id,
            status=status,
            matched=False,
            reason="must_missed",
            tokens_hit=tuple(must_hits),
            tokens_missed=tuple(must_missed),
        )

    any_of = screen.get("any_of") or {"min_hits": 0, "tokens": []}
    min_hits = int(any_of.get("min_hits") or 0)
    any_tokens = [Anchor(str(t)) for t in (any_of.get("tokens") or [])]
    any_hits = _hit_texts(ocr, any_tokens, min_confidence=min_confidence)
    any_missed = [a.text for a in any_tokens if a.text not in any_hits]
    if len(any_hits) < min_hits:
        return ScreenMatch(
            screen_id=screen_id,
            status=status,
            matched=False,
            reason="any_of_insufficient",
            tokens_hit=tuple([*must_hits, *any_hits]),
            tokens_missed=tuple(any_missed),
            any_of_hits=tuple(any_hits),
            any_of_min_hits=min_hits,
        )

    return ScreenMatch(
        screen_id=screen_id,
        status=status,
        matched=True,
        reason="matched",
        tokens_hit=tuple([*must_hits, *any_hits]),
        tokens_missed=(),
        any_of_hits=tuple(any_hits),
        any_of_min_hits=min_hits,
    )


def match_screens(
    ocr: dict[str, Any],
    screens: Iterable[dict[str, Any]],
    *,
    statuses: tuple[str, ...] = ("active",),
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> ScreenMatch | None:
    """Return the single matched screen, None for unknown, or fail on conflicts."""
    results = [
        match_screen(ocr, screen, min_confidence=min_confidence)
        for screen in screens
        if str(screen.get("status") or "") in statuses
    ]
    matched = [result for result in results if result.matched]
    if not matched:
        return None
    if len(matched) > 1:
        ids = ", ".join(result.screen_id for result in matched)
        raise ScreenSignatureConflict(f"多个 screen 签名同时命中：{ids}")
    return matched[0]


def match_screen_asset(
    ocr: dict[str, Any],
    path: Path,
    *,
    statuses: tuple[str, ...] = ("active",),
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> ScreenMatch | None:
    """Load an asset file and match its screens against one OCR frame."""
    doc = load_screen_asset(path)
    return match_screens(
        ocr,
        doc.get("screens") or [],
        statuses=statuses,
        min_confidence=min_confidence,
    )


def _normalize_detect_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _ocr_has_text(ocr: dict[str, Any] | None, text: str, *, min_confidence: float) -> bool:
    if not isinstance(ocr, dict):
        return False
    for box in ocr.get("boxes") or []:
        if not isinstance(box, dict):
            continue
        if text not in str(box.get("text") or ""):
            continue
        if float(box.get("confidence") or 0.0) >= min_confidence:
            return True
    return False


def _ax_has_text(ax_dump: Any, text: str) -> bool:
    if ax_dump is None:
        return False
    nodes = ax_dump.get("nodes") if isinstance(ax_dump, dict) else getattr(ax_dump, "nodes", None)
    if not nodes:
        return False
    for node in nodes:
        if isinstance(node, dict):
            label = str(node.get("label") or "")
            value = str(node.get("value") or "")
            identifier = str(node.get("identifier") or "")
        else:
            label = str(getattr(node, "label", "") or "")
            value = str(getattr(node, "value", None) or "")
            identifier = str(getattr(node, "identifier", "") or "")
        if text in label or text in value or text in identifier:
            return True
    return False


def detect_blocking_overlay(
    ocr: dict[str, Any] | None,
    screen_def: dict[str, Any] | None,
    *,
    ax_dump: Any = None,
    min_confidence: float = 0.3,
) -> dict[str, Any] | None:
    """Return the first matching ``blocking_overlays`` entry on ``screen_def``, or ``None``.

    Pure and fast: reuses the caller's ``ocr`` frame and optional ``ax_dump`` with
    zero device I/O. Supports ``detect.ocr_text``, ``detect.text``, and
    ``detect.ax_text`` as either ``str`` or ``list[str]`` (all declared tokens in
    each modality must match).
    """
    if not isinstance(screen_def, dict):
        return None
    for overlay in screen_def.get("blocking_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        detect = overlay.get("detect")
        if not isinstance(detect, dict) or not detect:
            continue
        ocr_texts = [
            *_normalize_detect_texts(detect.get("ocr_text")),
            *_normalize_detect_texts(detect.get("text")),
        ]
        ax_texts = _normalize_detect_texts(detect.get("ax_text"))
        if not ocr_texts and not ax_texts:
            continue
        if ocr_texts and (
            ocr is None
            or not all(_ocr_has_text(ocr, text, min_confidence=min_confidence) for text in ocr_texts)
        ):
            continue
        if ax_texts and (
            ax_dump is None or not all(_ax_has_text(ax_dump, text) for text in ax_texts)
        ):
            continue
        return overlay
    return None


def summarize_checkpoint_telemetry(
    rows: Iterable[dict[str, Any]],
    *,
    stale_threshold: float = 0.5,
    min_samples: int = 3,
) -> dict[str, Any]:
    """Aggregate S4 checkpoint telemetry for S5 tuning/staleness signals.

    This is deliberately advisory: it reports token hit/miss rates and
    unknown-screen spikes, but does not mutate screen assets or promote/demote
    tokens automatically.
    """
    by_screen: dict[str, dict[str, Any]] = {}
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        expected = str(row.get("expected") or "")
        if expected:
            buckets[expected].append(row)

    for screen_id, items in sorted(buckets.items()):
        reason_counts = Counter(str(item.get("reason") or "") for item in items)
        token_hit = Counter()
        token_miss = Counter()
        for item in items:
            token_hit.update(str(t) for t in (item.get("tokens_hit") or []))
            token_miss.update(str(t) for t in (item.get("tokens_missed") or []))
        tokens: dict[str, dict[str, Any]] = {}
        for token in sorted(set(token_hit) | set(token_miss)):
            hit = token_hit[token]
            miss = token_miss[token]
            total = hit + miss
            tokens[token] = {
                "hit": hit,
                "miss": miss,
                "hit_rate": hit / total if total else 0.0,
                "miss_rate": miss / total if total else 0.0,
            }
        total = len(items)
        unknown = reason_counts.get("unknown_screen", 0)
        no_match_rate = unknown / total if total else 0.0
        by_screen[screen_id] = {
            "screen_id": screen_id,
            "samples": total,
            "passed": reason_counts.get("matched", 0),
            "wrong_page": reason_counts.get("wrong_page", 0),
            "unknown_screen": unknown,
            "checkpoint_error": reason_counts.get("checkpoint_error", 0),
            "no_match_rate": no_match_rate,
            "signature_stale": total >= min_samples and no_match_rate >= stale_threshold,
            "tokens": tokens,
        }
    return {"screens": by_screen, "total_checkpoints": sum(len(v) for v in buckets.values())}


def checkpoint_rows_from_operation_results(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract screen checkpoint rows from operation-result-like payloads."""
    rows: list[dict[str, Any]] = []
    for result in results:
        run_id = result.get("run_id")
        build = result.get("build") or result.get("build_label")
        for step in result.get("steps") or []:
            checkpoint = step.get("screen_checkpoint") if isinstance(step, dict) else None
            if not isinstance(checkpoint, dict):
                continue
            row = dict(checkpoint)
            row["run_id"] = run_id
            row["step_index"] = step.get("index")
            if build is not None:
                row["build"] = build
            rows.append(row)
    return rows


def _screen_by_id(doc: dict[str, Any], screen_id: str) -> dict[str, Any]:
    return screen_by_id(doc, screen_id)


def _valid_region_box(box: Any) -> bool:
    rect = box.get("rect") if isinstance(box, dict) else box
    if not (isinstance(rect, list) and len(rect) == 4):
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in rect):
        return False
    nx0, ny0, nx1, ny1 = [float(v) for v in rect]
    return 0.0 <= nx0 < nx1 <= 1.0 and 0.0 <= ny0 < ny1 <= 1.0


def _region_rect(box: Any) -> tuple[float, float, float, float]:
    rect = box.get("rect") if isinstance(box, dict) else box
    return tuple(float(v) for v in rect)  # type: ignore[return-value]


def _telemetry_dict(row: dict[str, Any] | ScreenMatch) -> dict[str, Any]:
    return row.as_dict() if isinstance(row, ScreenMatch) else dict(row)


def _telemetry_screen_id(row: dict[str, Any] | ScreenMatch) -> str | None:
    if isinstance(row, ScreenMatch):
        return row.screen_id
    return row.get("screen_id") or row.get("expected") or row.get("matched")


def _telemetry_has(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    return bool(value)


def _telemetry_clean_match(row: dict[str, Any]) -> bool:
    matched = row.get("matched")
    expected = row.get("expected")
    matched_ok = matched is True
    if isinstance(matched, str) and matched:
        matched_ok = (
            str(row.get("status") or "") == "passed"
            and str(row.get("reason") or "matched") == "matched"
            and (not expected or matched == expected)
        )
    return (
        matched_ok
        and str(row.get("reason") or "matched") == "matched"
        and not _telemetry_has(row, "tokens_missed")
        and not _telemetry_has(row, "forbid_hit")
    )


def _anchors(raw: Iterable[Any]) -> list[Anchor]:
    out: list[Anchor] = []
    for item in raw:
        if isinstance(item, str):
            out.append(Anchor(item))
        elif isinstance(item, dict):
            out.append(Anchor(str(item["text"]), item.get("where"), item.get("kind")))
        else:  # JSON schema normally catches this; keep direct callers fail-loud too.
            raise ScreenAssetError(f"非法 anchor：{item!r}")
    return out


def _hit_texts(ocr: dict[str, Any], anchors: list[Anchor], *, min_confidence: float) -> list[str]:
    hits: list[str] = []
    for anchor in anchors:
        if _find_anchor(ocr, anchor, min_confidence=min_confidence):
            hits.append(anchor.text)
    return hits


def _find_anchor(ocr: dict[str, Any], anchor: Anchor, *, min_confidence: float) -> TokenHit | None:
    iw = float(ocr.get("image_w") or 0)
    ih = float(ocr.get("image_h") or 0)
    if iw <= 0 or ih <= 0:
        raise ScreenAssetError("OCR 结果缺少有效 image_w/image_h")
    if anchor.where and anchor.where not in WHERE_REGIONS:
        raise ScreenAssetError(f"未知 where：{anchor.where}")
    region = WHERE_REGIONS.get(anchor.where or "") if anchor.where else None
    matches: list[TokenHit] = []
    for box in ocr.get("boxes") or []:
        box_text = str(box.get("text") or "")
        if anchor.text not in box_text:
            continue
        confidence = float(box.get("confidence") or 0.0)
        if confidence < min_confidence:
            continue
        nx = (float(box.get("x") or 0.0) + float(box.get("w") or 0.0) / 2.0) / iw
        ny = (float(box.get("y") or 0.0) + float(box.get("h") or 0.0) / 2.0) / ih
        if region and not (region[0] <= nx <= region[2] and region[1] <= ny <= region[3]):
            continue
        matches.append(TokenHit(anchor.text, anchor.where, box_text, confidence, (nx, ny)))
    matches.sort(key=lambda hit: hit.confidence, reverse=True)
    return matches[0] if matches else None
