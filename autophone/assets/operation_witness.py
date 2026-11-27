"""Semantic identity of an Operation contract used by witness evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def operation_identity(app: str, definition: Mapping[str, Any]) -> dict[str, Any]:
    """Bind App, stable ID, revision, parameters, executable steps and witness."""
    if (not app or not definition.get("id") or not definition.get("name")
            or type(definition.get("revision")) is not int or definition["revision"] < 1):
        raise ValueError("Operation identity requires app, id, name and positive revision")
    semantic = {key: definition.get(key) for key in ("params", "steps", "witness")}
    digest = hashlib.sha256(json.dumps(
        semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {"app": app, "id": definition["id"], "name": definition["name"],
            "revision": definition["revision"], "content_sha256": digest}
