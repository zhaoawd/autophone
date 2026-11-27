"""Impact gap triage: turn a flat gap list into evidence-shaped review queues.

The triage document is derived from an Impact Index. It does not dismiss gaps,
create FieldBindings, or infer that an endpoint causes a screen region. The
classifications only describe observable transport and coverage properties.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping


TRIAGE_SCHEMA_VERSION = "v1"

_MEDIA_EXTENSIONS = frozenset({
    ".avif", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".webp",
})
_CONFIG_EXTENSIONS = frozenset({".json", ".plist"})


def _path_suffix(endpoint_key: str) -> str:
    try:
        _method, location = str(endpoint_key).split(" ", 1)
    except ValueError:
        return ""
    slash = location.find("/")
    path = location[slash:] if slash >= 0 else "/"
    return PurePosixPath(path.lower()).suffix


def _endpoint_category(endpoint_key: str, row: Mapping[str, Any]) -> str:
    suffix = _path_suffix(endpoint_key)
    if suffix in _MEDIA_EXTENSIONS:
        return "static_media_candidate"
    if suffix in _CONFIG_EXTENSIONS:
        return "static_config_candidate"

    screens = {str(item) for item in (row.get("screens") or []) if item}
    known_screens = screens - {"unknown"}
    if not known_screens:
        return "unknown_screen_candidate"
    if len(known_screens) > 1 or "unknown" in screens:
        return "multi_screen_candidate"
    if row.get("response_paths"):
        return "screen_scoped_response_candidate"
    return "screen_scoped_request_candidate"


def build(index_doc: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic, non-causal triage projection from an index."""
    gaps = list(index_doc.get("gaps") or [])
    observed = dict(index_doc.get("observed_endpoints") or {})
    endpoint_keys = sorted({
        str(gap.get("endpoint_key") or "")
        for gap in gaps
        if gap.get("kind") == "unbound_endpoint" and gap.get("endpoint_key")
    })

    endpoint_rows: list[dict[str, Any]] = []
    endpoint_counts: dict[str, int] = {}
    for key in endpoint_keys:
        source = dict(observed.get(key) or {})
        category = _endpoint_category(key, source)
        endpoint_counts[category] = endpoint_counts.get(category, 0) + 1
        response_paths = sorted({str(path) for path in (source.get("response_paths") or [])})
        endpoint_rows.append({
            "endpoint_key": key,
            "category": category,
            "observations": int(source.get("observations") or 0),
            "noise_observations": int(source.get("noise") or 0),
            "screens": sorted({str(item) for item in (source.get("screens") or []) if item}),
            "triggers": dict(sorted(dict(source.get("triggers") or {}).items())),
            "response_path_count": len(response_paths),
            "response_path_sample": response_paths[:20],
        })

    observed_screens = {
        str(row.get("screen_id") or "")
        for row in dict(index_doc.get("screen_states") or {}).values()
        if row.get("screen_id") and row.get("screen_id") != "unknown"
    }
    screen_rows: list[dict[str, Any]] = []
    screen_counts: dict[str, int] = {}
    for screen_id in sorted({
        str(gap.get("screen_id") or "")
        for gap in gaps
        if gap.get("kind") == "unbound_screen" and gap.get("screen_id")
    }):
        category = "observed_unbound" if screen_id in observed_screens else "not_observed"
        screen_counts[category] = screen_counts.get(category, 0) + 1
        screen_rows.append({"screen_id": screen_id, "category": category})

    return {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        "app": str(index_doc.get("app") or ""),
        "build": str(index_doc.get("build") or ""),
        "source_generated_at": index_doc.get("generated_at"),
        "policy": {
            "creates_bindings": False,
            "dismisses_gaps": False,
            "claims_causality": False,
        },
        "counts": {
            "unbound_endpoints": len(endpoint_rows),
            "unbound_screens": len(screen_rows),
            "endpoint_categories": dict(sorted(endpoint_counts.items())),
            "screen_categories": dict(sorted(screen_counts.items())),
        },
        "endpoints": endpoint_rows,
        "screens": screen_rows,
    }
