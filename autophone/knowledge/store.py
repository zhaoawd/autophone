"""Reviewable YAML source of truth for Knowledge Claims."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.assets.policy import assert_assets_writable
from autophone.assets.store import path_write_lock
from autophone.contracts import validate_or_raise
from autophone.util import write_text_atomic


def claims_path(app: str, *, root: Path = APP_ASSET_ROOT) -> Path:
    return root / "apps" / app / "knowledge" / "claims.yaml"


def load_claims(app: str, *, root: Path = APP_ASSET_ROOT) -> dict[str, Any]:
    path = claims_path(app, root=root)
    if not path.exists():
        return {"schema_version": "v1", "app": app, "claims": []}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    validate_or_raise("knowledge-claims.v1.json", raw)
    return raw


def save_claims(app: str, claims: Iterable[dict[str, Any]], *, root: Path = APP_ASSET_ROOT) -> Path:
    path = claims_path(app, root=root)
    assert_assets_writable("write App knowledge claims", path=path)
    with path_write_lock(path):
        payload = {"schema_version": "v1", "app": app, "claims": list(claims)}
        validate_or_raise("knowledge-claims.v1.json", payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        write_text_atomic(path, text)
    return path


class ClaimExistsError(ValueError):
    """A Claim with the same ID is already in the store; it is never replaced."""


def append_draft(app: str, claim: dict[str, Any], *, root: Path = APP_ASSET_ROOT) -> Path:
    """Append one new draft Claim under the store's write lock (BL-038 D3).

    Existing Claims are written back as loaded; an existing ID raises
    ``ClaimExistsError`` instead of being skipped or replaced.
    """
    if claim.get("status") != "draft":
        raise ValueError("only draft Claims can be appended")
    path = claims_path(app, root=root)
    assert_assets_writable("write App knowledge claims", path=path)
    with path_write_lock(path):
        current = load_claims(app, root=root)
        if current.get("app") != app:
            raise ValueError(f"claims.yaml app {current.get('app')!r} does not match {app!r}")
        if any(str(row.get("id")) == str(claim.get("id")) for row in current["claims"]):
            raise ClaimExistsError(str(claim.get("id")))
        return save_claims(app, [*current["claims"], claim], root=root)


def merge_drafts(app: str, drafts: Iterable[dict[str, Any]], *, root: Path = APP_ASSET_ROOT) -> Path:
    path = claims_path(app, root=root)
    assert_assets_writable("write App knowledge claims", path=path)
    with path_write_lock(path):
        current = load_claims(app, root=root)
        by_id = {str(row.get("id")): row for row in current["claims"]}
        for draft in drafts:
            claim_id = str(draft.get("id") or "")
            if claim_id and claim_id not in by_id:
                by_id[claim_id] = draft
        return save_claims(app, by_id.values(), root=root)
