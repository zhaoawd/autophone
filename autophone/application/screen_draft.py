"""S1 screen draft generation from OCR samples.

The AI-facing part of S1 names/classifies screenshots. The deterministic half
then computes cross-sample token frequencies and emits a ``status: draft``
screen asset. Stability comes from data, not from a single-frame guess.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from autophone.assets.policy import assert_assets_writable
from autophone.application.ai import AiRequest, AiResult, invoke
from autophone.assets import screens
from autophone.logging import redact_ocr_texts


class ScreenDraftError(ValueError):
    """Screen draft generation failed."""


@dataclass(frozen=True)
class OcrSample:
    screen_id: str
    ocr: dict[str, Any]
    source: str = ""


_CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["assignments"],
    "additionalProperties": False,
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["sample_id", "screen_id"],
                "additionalProperties": False,
                "properties": {
                    "sample_id": {"type": "string", "minLength": 1},
                    "screen_id": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_CLASSIFY_SYSTEM = """\
You classify mobile app screenshots into stable logical screens from OCR text.
Return only structured JSON matching the schema. Use concise lower_snake_case
screen_id values. Assign the same screen_id to samples that are the same
logical screen even when product/content text changes. Prefer persistent chrome,
tabs, titles, buttons, and navigation text over one-off content.
"""


def classify_ocr_samples_with_ai(
    samples: Iterable[dict[str, Any]],
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 120.0,
    invoker=invoke,
) -> list[OcrSample]:
    """Classify raw OCR samples into screen ids using the platform AI seam.

    The model only assigns ``sample_id -> screen_id``. Draft generation remains
    deterministic and is handled by :func:`draft_screens_from_ocr_samples`.
    """
    prepared: list[tuple[str, dict[str, Any], str]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(samples, start=1):
        sample_id, ocr, source = _coerce_raw_ocr_sample(raw, idx)
        if sample_id in seen_ids:
            raise ScreenDraftError(f"duplicate sample_id: {sample_id}")
        seen_ids.add(sample_id)
        _validate_ocr(ocr)
        prepared.append((sample_id, ocr, source))
    if not prepared:
        raise ScreenDraftError("no samples")

    prompt = _build_classify_prompt(prepared)
    res: AiResult = invoker(AiRequest(
        prompt=prompt,
        system=_CLASSIFY_SYSTEM,
        tools="none",
        json_schema=_CLASSIFY_SCHEMA,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
    ))
    if not res.ok or not isinstance(res.json, dict):
        raise ScreenDraftError(res.error or "screen classifier returned no structured assignments")
    assignments = res.json.get("assignments")
    if not isinstance(assignments, list):
        raise ScreenDraftError("screen classifier result lacks assignments")

    by_id: dict[str, str] = {}
    for row in assignments:
        if not isinstance(row, dict):
            raise ScreenDraftError("screen classifier assignment must be an object")
        sample_id = str(row.get("sample_id") or "")
        screen_id = str(row.get("screen_id") or "").strip()
        if not sample_id:
            raise ScreenDraftError("screen classifier assignment lacks sample_id")
        if not screen_id:
            raise ScreenDraftError(f"screen classifier assigned empty screen_id for {sample_id}")
        if sample_id not in seen_ids:
            raise ScreenDraftError(f"screen classifier returned unknown sample_id: {sample_id}")
        if sample_id in by_id:
            raise ScreenDraftError(f"screen classifier returned duplicate assignment: {sample_id}")
        by_id[sample_id] = screen_id

    missing = [sample_id for sample_id, _, _ in prepared if sample_id not in by_id]
    if missing:
        raise ScreenDraftError(f"screen classifier missing assignments: {', '.join(missing)}")

    return [
        OcrSample(screen_id=by_id[sample_id], ocr=ocr, source=source or sample_id)
        for sample_id, ocr, source in prepared
    ]


def draft_screens_from_ocr_samples(
    samples: Iterable[OcrSample | dict[str, Any]],
    *,
    build: str,
    app: str | None = None,
    min_samples: int = 1,
    must_threshold: float = 1.0,
    any_threshold: float = 0.5,
    forbid_threshold: float = 1.0,
    any_min_hits: int = 1,
) -> dict[str, Any]:
    """Build a S1 draft screen asset from labeled OCR samples.

    ``screen_id`` is the output of the AI/human classification step. Tokens seen
    in nearly every sample of a screen become ``must`` candidates; tokens seen
    often but below ``must_threshold`` become ``any_of`` candidates; tokens stable
    on another screen and absent from this one become conservative ``forbid``
    candidates. All screens remain ``draft`` for human review and later S2 gates.
    """
    if not build:
        raise ScreenDraftError("build is required")
    if min_samples < 1:
        raise ScreenDraftError("min_samples must be >= 1")
    for name, value in {
        "must_threshold": must_threshold,
        "any_threshold": any_threshold,
        "forbid_threshold": forbid_threshold,
    }.items():
        if not (0.0 < value <= 1.0):
            raise ScreenDraftError(f"{name} must be in (0, 1]")
    if any_threshold > must_threshold:
        raise ScreenDraftError("any_threshold must be <= must_threshold")
    if any_min_hits < 0:
        raise ScreenDraftError("any_min_hits must be >= 0")

    grouped: dict[str, list[OcrSample]] = defaultdict(list)
    for raw in samples:
        sample = _coerce_sample(raw)
        if not sample.screen_id:
            raise ScreenDraftError("sample.screen_id is required")
        _validate_ocr(sample.ocr)
        grouped[sample.screen_id].append(sample)
    if not grouped:
        raise ScreenDraftError("no samples")

    token_counts: dict[str, Counter[str]] = {}
    token_positions: dict[str, dict[str, list[tuple[float, float]]]] = {}
    sample_counts: dict[str, int] = {}
    for screen_id, rows in grouped.items():
        if len(rows) < min_samples:
            raise ScreenDraftError(
                f"{screen_id} samples={len(rows)} below min_samples={min_samples}"
            )
        sample_counts[screen_id] = len(rows)
        token_counts[screen_id] = Counter()
        token_positions[screen_id] = defaultdict(list)
        for sample in rows:
            seen: set[str] = set()
            for token, center in _ocr_tokens(sample.ocr):
                seen.add(token)
                token_positions[screen_id][token].append(center)
            token_counts[screen_id].update(seen)

    stable_by_screen: dict[str, set[str]] = {
        sid: {
            token
            for token, count in counts.items()
            if count / sample_counts[sid] >= forbid_threshold
        }
        for sid, counts in token_counts.items()
    }

    screen_docs: list[dict[str, Any]] = []
    for screen_id in sorted(grouped):
        count = sample_counts[screen_id]
        must: list[dict[str, str]] = []
        any_tokens: list[str] = []

        for token in sorted(token_counts[screen_id]):
            rate = token_counts[screen_id][token] / count
            if rate >= must_threshold:
                must.append(_anchor(token, token_positions[screen_id][token]))
            elif rate >= any_threshold:
                any_tokens.append(token)

        own_tokens = set(token_counts[screen_id])
        forbid_tokens: set[str] = set()
        forbid_positions: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for other_id, stable_tokens in stable_by_screen.items():
            if other_id == screen_id:
                continue
            for token in stable_tokens - own_tokens:
                forbid_tokens.add(token)
                forbid_positions[token].extend(token_positions[other_id][token])
        forbid = [
            _anchor(token, forbid_positions[token])
            for token in sorted(forbid_tokens)
        ]

        screen: dict[str, Any] = {
            "id": screen_id,
            "status": "draft",
            "revision": 1,
            "must": must,
            "forbid": forbid,
        }
        if any_tokens:
            screen["any_of"] = {
                "min_hits": min(any_min_hits, len(any_tokens)),
                "tokens": sorted(any_tokens),
            }
        else:
            screen["any_of"] = {"min_hits": 0, "tokens": []}
        screen_docs.append(screen)

    meta: dict[str, Any] = {
        "schema_version": 1,
        "build": build,
        "total": len(screen_docs),
        "next_seq": len(screen_docs) + 1,
    }
    if app:
        meta["app"] = app
        meta["title"] = f"{app} 屏骨架"
    doc = {"meta": meta, "screens": screen_docs}
    screens.validate_screen_asset(doc)
    return doc


def write_screen_draft(path: Path, doc: dict[str, Any], *, overwrite: bool = False) -> Path:
    """Write a generated draft asset after validating the S0 contract."""
    assert_assets_writable("write screen draft", path=path)
    screens.validate_screen_asset(doc, path=path)
    if path.exists() and not overwrite:
        raise ScreenDraftError(f"refusing to overwrite existing screen asset: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


def _coerce_sample(raw: OcrSample | dict[str, Any]) -> OcrSample:
    if isinstance(raw, OcrSample):
        return raw
    if not isinstance(raw, dict):
        raise ScreenDraftError(f"invalid sample: {raw!r}")
    ocr = raw.get("ocr") or raw.get("ocr_result")
    if not isinstance(ocr, dict):
        raise ScreenDraftError("sample.ocr must be an object")
    return OcrSample(screen_id=str(raw.get("screen_id") or ""), ocr=ocr, source=str(raw.get("source") or ""))


def _coerce_raw_ocr_sample(raw: dict[str, Any], idx: int) -> tuple[str, dict[str, Any], str]:
    if not isinstance(raw, dict):
        raise ScreenDraftError(f"invalid sample: {raw!r}")
    ocr = raw.get("ocr") or raw.get("ocr_result")
    if not isinstance(ocr, dict):
        raise ScreenDraftError("sample.ocr must be an object")
    sample_id = str(
        raw.get("sample_id")
        or raw.get("id")
        or raw.get("source")
        or f"sample_{idx:03d}"
    )
    source = str(raw.get("source") or sample_id)
    return sample_id, ocr, source


def _build_classify_prompt(samples: list[tuple[str, dict[str, Any], str]]) -> str:
    compact = [
        {
            "sample_id": sample_id,
            "source": source,
            "tokens": _summarize_ocr(ocr),
        }
        for sample_id, ocr, source in samples
    ]
    return (
        "Classify these OCR samples into stable app screen ids. "
        "Return one assignment for every input sample_id.\n\n"
        f"{yaml.safe_dump({'samples': compact}, allow_unicode=True, sort_keys=False, width=100)}"
    )


def _summarize_ocr(ocr: dict[str, Any]) -> list[dict[str, Any]]:
    """Prompt-only OCR summary; text passes the OCR redaction before it reaches the model.

    The whole frame is redacted at once so OCR-misread phone fragments can be matched
    against the frame's own phone numbers. The draft itself still uses the raw OCR.
    ``known_numbers`` is not passed: production has no confirmed phone number set.
    """
    tokens = _ocr_tokens(ocr)
    redacted = redact_ocr_texts([token for token, _ in tokens])
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for text, (_token, center) in zip(redacted, tokens):
        grouped[text].append(center)
    out = [{"text": text, "where": _where_for_positions(positions)} for text, positions in grouped.items()]
    return sorted(out, key=lambda row: (row["where"], row["text"]))


def _validate_ocr(ocr: dict[str, Any]) -> None:
    if float(ocr.get("image_w") or 0) <= 0 or float(ocr.get("image_h") or 0) <= 0:
        raise ScreenDraftError("OCR sample lacks valid image_w/image_h")
    if not isinstance(ocr.get("boxes"), list):
        raise ScreenDraftError("OCR sample boxes must be a list")


def _ocr_tokens(ocr: dict[str, Any]) -> list[tuple[str, tuple[float, float]]]:
    iw = float(ocr["image_w"])
    ih = float(ocr["image_h"])
    out: list[tuple[str, tuple[float, float]]] = []
    for box in ocr.get("boxes") or []:
        token = _normalize_text(box.get("text"))
        if not token:
            continue
        nx = (float(box.get("x") or 0.0) + float(box.get("w") or 0.0) / 2.0) / iw
        ny = (float(box.get("y") or 0.0) + float(box.get("h") or 0.0) / 2.0) / ih
        out.append((token, (nx, ny)))
    return out


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _anchor(token: str, positions: list[tuple[float, float]]) -> dict[str, str]:
    anchor = {"text": token, "where": _where_for_positions(positions), "kind": "chrome_candidate"}
    return anchor


def _where_for_positions(positions: list[tuple[float, float]]) -> str:
    if not positions:
        return "content"
    xs = sorted(p[0] for p in positions)
    ys = sorted(p[1] for p in positions)
    nx = xs[len(xs) // 2]
    ny = ys[len(ys) // 2]
    if ny <= 0.20:
        return "top"
    if ny >= 0.80:
        return "bottom"
    if nx <= 0.30:
        return "left-rail"
    if nx >= 0.70:
        return "right-rail"
    return "content"
