"""Record the Recipe identity each Case actually executed (BL-186).

Before execution the platform reads, once, the Recipe bytes every runnable Case
resolves to: the frozen draft document for an explicit Draft Trial, otherwise
the live ``recipes/<id>.yaml``.  For a live Recipe the bytes are the ones
``RunExecution._derive_case_requirements`` read to plan capabilities
(``RECIPE_READ_KEY``), so the recorded digest and the planned requirements
come from the same read.  The platform keeps the file SHA-256 and the
type-tagged canonical digest of the parsed steps.  The Runner reports the
canonical digest of the steps it executed (``CaseOutcome.recipe_steps_digest``).
After execution the identity is attached with ``recipe_sha256`` only when both
digests are equal; otherwise ``recipe_sha256`` is null and
``unverified_reason`` says why.

The identity is therefore taken from content loaded during the run, never
recomputed from the files after the run.  Execution is unchanged: the Runner
still resolves steps from the same App-scoped registry and draft overrides,
and no failure here stops a Case from running.
"""
from __future__ import annotations

import copy
import hashlib
import logging
from pathlib import Path
from typing import Any, Iterable

import yaml

from autophone.assets.apps import AppProfile
from autophone.assets.promotion_identity import (
    REASON_PATH_OUTSIDE_APP,
    RECIPE_SOURCE_DRAFT,
    RECIPE_SOURCE_LIVE,
    case_binding_from_registry,
    executed_recipe_identity,
    load_case_registry,
    recipe_path,
)
from autophone.contracts import ContractError
from autophone.execution.engine.recipe import recipe_steps_digest

logger = logging.getLogger(__name__)

#: Private Case plan key written by ``_derive_case_requirements``: the digests
#: of the live Recipe bytes it read.  Holds strings only (no host path).
RECIPE_READ_KEY = "_recipe_read"

#: ``unverified_reason`` values.  Each keeps ``recipe_sha256`` null.
REASON_FILE_MISSING = "recipe_file_missing"
REASON_FILE_UNREADABLE = "recipe_file_unreadable"
REASON_FILE_INVALID = "recipe_file_invalid"
REASON_CASE_BINDING_INVALID = "case_binding_invalid"
REASON_EXECUTED_STEPS_DIFFER = "executed_steps_differ_from_recipe_file"
REASON_OUTSIDE_APP = REASON_PATH_OUTSIDE_APP


def recipe_read_record(
    profile: AppProfile,
    case_id: str,
    path: Path,
    data: bytes,
    document: Any,
) -> dict[str, Any]:
    """Digests of Recipe bytes read for planning, for identity recording."""
    live = Path(path) == recipe_path(profile, case_id, source=RECIPE_SOURCE_LIVE)
    steps = document.get("steps") if isinstance(document, dict) else None
    try:
        steps_digest = recipe_steps_digest(steps) if isinstance(steps, list) and steps else None
    except Exception:  # noqa: BLE001 - identity evidence must never block planning
        steps_digest = None
    return {
        "source": RECIPE_SOURCE_LIVE if live else "other",
        "sha256": hashlib.sha256(data).hexdigest(),
        "steps_digest": steps_digest,
    }


def recipe_identity_candidates(
    profile: AppProfile,
    cases: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collect the Recipe content each Case would run, before it runs.

    Returns ``{CASE_ID: {"identity": ..., "steps_digest": str | None}}``.
    Nothing here raises: a Case whose candidate cannot be built is recorded
    as ``recipe_file_invalid`` and still runs.
    """
    candidates: dict[str, dict[str, Any]] = {}
    registry: dict[str, Any] | None = None
    registry_loaded = False
    for case in cases:
        key = str(case.get("id") or "").upper()
        if not key or key in candidates or case.get("_trial_recipe_missing") is True:
            continue
        try:
            trial_identity = case.get("_trial_recipe_identity")
            trial_document = case.get("_trial_recipe_document")
            if isinstance(trial_identity, dict) and isinstance(trial_document, dict):
                candidates[key] = _draft_candidate(profile, key, trial_identity, trial_document)
                continue
            if not registry_loaded:
                registry_loaded = True
                try:
                    registry = load_case_registry(profile)
                except Exception:  # noqa: BLE001 - recorded per Case as binding invalid
                    registry = None
            candidates[key] = _live_candidate(profile, key, case, registry)
        except Exception:  # noqa: BLE001 - identity evidence must never block a Run
            logger.warning("用例 %s 的 Recipe 执行身份候选读取失败，记为未核对", key, exc_info=True)
            candidates[key] = _failed_candidate(profile, key, case)
    return candidates


def _draft_candidate(
    profile: AppProfile,
    key: str,
    trial_identity: dict[str, Any],
    trial_document: dict[str, Any],
) -> dict[str, Any]:
    # The draft bytes were hashed and parsed once when the Trial froze.
    identity = executed_recipe_identity(
        profile,
        key,
        source=RECIPE_SOURCE_DRAFT,
        recipe_sha256=trial_identity.get("draft_recipe_sha256"),
        case_revision=trial_identity.get("case_revision"),
        reserved_external_id=trial_identity.get("reserved_external_id"),
    )
    return {"identity": identity, "steps_digest": recipe_steps_digest(trial_document.get("steps"))}


def _failed_candidate(profile: AppProfile, key: str, case: dict[str, Any]) -> dict[str, Any]:
    source = (
        RECIPE_SOURCE_DRAFT
        if isinstance(case.get("_trial_recipe_identity"), dict)
        else RECIPE_SOURCE_LIVE
    )
    revision = case.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        revision = None
    try:
        identity = executed_recipe_identity(
            profile, key, source=source, recipe_sha256=None, case_revision=revision,
            reserved_external_id=None, unverified_reason=REASON_FILE_INVALID,
        )
    except Exception:  # noqa: BLE001 - last-resort record without any path lookup
        prefix = "recipes/drafts" if source == RECIPE_SOURCE_DRAFT else "recipes"
        identity = {
            "source": source, "path": f"{prefix}/{key.lower()}.yaml", "recipe_sha256": None,
            "case_revision": revision, "reserved_external_id": None,
            "unverified_reason": REASON_FILE_INVALID,
        }
    return {"identity": identity, "steps_digest": None}


def _live_candidate(
    profile: AppProfile,
    key: str,
    case: dict[str, Any],
    registry: dict[str, Any] | None,
) -> dict[str, Any]:
    revision = case.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        revision = None
    reserved: str | None = None
    reason: str | None = None
    try:
        if registry is None:
            raise ContractError("Case Registry unreadable")
        revision, reserved = case_binding_from_registry(registry, key)
    except ContractError:
        reason = REASON_CASE_BINDING_INVALID
    digest: str | None = None
    steps_digest: str | None = None
    if reason is None:
        read = case.get(RECIPE_READ_KEY)
        if isinstance(read, dict) and read.get("source") == RECIPE_SOURCE_LIVE:
            # Same bytes the capability planner read for this Case.
            digest = read.get("sha256")
            steps_digest = read.get("steps_digest")
            if steps_digest is None:
                digest, reason = None, REASON_FILE_INVALID
        else:
            digest, steps_digest, reason = _read_live_file(profile, key)
    identity = executed_recipe_identity(
        profile,
        key,
        source=RECIPE_SOURCE_LIVE,
        recipe_sha256=digest,
        case_revision=revision,
        reserved_external_id=reserved,
        unverified_reason=reason,
    )
    return {"identity": identity, "steps_digest": steps_digest}


def _read_live_file(profile: AppProfile, key: str) -> tuple[str | None, str | None, str | None]:
    """Read a live Recipe when the planner did not (returns digest, steps digest, reason)."""
    path = recipe_path(profile, key, source=RECIPE_SOURCE_LIVE)
    if not path.is_file():
        return None, None, REASON_FILE_MISSING
    try:
        data = path.read_bytes()
    except OSError:
        return None, None, REASON_FILE_UNREADABLE
    try:
        document = yaml.safe_load(data.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        return None, None, REASON_FILE_INVALID
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list) or not steps:
        return None, None, REASON_FILE_INVALID
    return hashlib.sha256(data).hexdigest(), recipe_steps_digest(steps), None


def attach_recipe_identities(
    outcomes: Iterable[Any],
    candidates: dict[str, dict[str, Any]],
) -> None:
    """Attach the executed Recipe identity to every Case that ran a Recipe."""
    for outcome in outcomes:
        if getattr(outcome, "exec_via", "") != "recipe":
            continue
        candidate = candidates.get(str(getattr(outcome, "id", "")).upper())
        if candidate is None:
            continue
        identity = copy.deepcopy(candidate["identity"])
        executed = getattr(outcome, "recipe_steps_digest", None)
        if "unverified_reason" not in identity and (
            candidate.get("steps_digest") is None
            or executed is None
            or executed != candidate["steps_digest"]
        ):
            identity["recipe_sha256"] = None
            identity["unverified_reason"] = REASON_EXECUTED_STEPS_DIFFER
        outcome.recipe_identity = identity
        if (getattr(outcome, "failure_in_recipe", False)
                and getattr(outcome, "status", "") in {"fail", "error", "blocked"}):
            from autophone.execution.failure_context import definition_identity
            outcome.failed_definition = (
                definition_identity("recipe", str(outcome.id), identity.get("case_revision"))
                if identity.get("recipe_sha256") and "unverified_reason" not in identity
                else {"status": "unknown"}
            )
