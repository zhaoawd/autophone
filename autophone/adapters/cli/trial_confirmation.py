"""Read the preparation cards explicitly acknowledged by a CLI caller."""
from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
from typing import Any


def load_trial_confirmations(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Preserve reviewed markers; submission and execution validate their freshness."""
    markers = []
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{path}: preparation card must be an object")
        marker = document.get("trial_confirmation", document)
        if not isinstance(marker, dict):
            raise ValueError(f"{path}: trial_confirmation must be an object")
        markers.append({**marker, "acknowledged": True})
    return markers
