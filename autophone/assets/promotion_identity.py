"""Frozen Case revision, reservation and Recipe identity.

Two identity forms exist:

- the draft identity (``case_revision``, ``reserved_external_id``,
  ``draft_recipe_sha256``) frozen by explicit Draft Trials and consumed by the
  Promotion Gate;
- the executed Recipe identity (BL-186) recorded for every Case that ran a
  Recipe: ``source`` (``live`` / ``draft``), App-relative ``path``,
  ``recipe_sha256`` of the bytes the run loaded, plus the same
  ``case_revision`` and ``reserved_external_id`` fields.

``run_matches_recipe_identity`` is the single judgement the preparation card
and the Promotion Gate share.  For a draft identity it delegates to
``trial_matches_identity`` so draft evidence keeps its existing rule.
"""

from __future__ import annotations
import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from autophone.assets.apps import AppProfile
from autophone.assets.negative_control import (
    canonical_artifact_content_digest,
    normalize_sha256,
)
from autophone.contracts import ContractError, validate_or_raise

_LABEL_BUILD_RE = re.compile(r"^(?P<short>.+)-(?P<build>bld-[0-9a-f]{12,64})$")
_APP_SYMBOLS_SCHEMA = "autophone.app-symbols/1"

RECIPE_SOURCE_LIVE = "live"
RECIPE_SOURCE_DRAFT = "draft"
RECIPE_SOURCES = (RECIPE_SOURCE_LIVE, RECIPE_SOURCE_DRAFT)

#: ``path`` prefix used when the Recipe directory is outside the App directory.
#: The host path is never recorded; the identity is then unverified.
OUTSIDE_APP_PATH_PREFIX = "<outside-app>"
REASON_PATH_OUTSIDE_APP = "recipe_path_outside_app"


def selector_is_draft_trial(selector: Any) -> bool:
    """Whether a Run's case selector is an explicit Draft Trial."""
    if not isinstance(selector, dict):
        return False
    statuses = selector.get("statuses") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    return "draft" in [str(item) for item in statuses] or bool(
        selector.get("trial_draft_recipes")
    )


def selector_names_case(selector: Any, case_id: str) -> bool:
    """Whether a Run's case selector names the Case explicitly in ``ids``.

    Matches ``case_selection.select_cases``: ``ids`` (a string or a list) is
    compared by exact Case ID string.  Selection by tag, module, suite, flow or
    status alone does not name a Case.
    """
    if not isinstance(selector, dict):
        return False
    ids = selector.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, (list, tuple)):
        return False
    return str(case_id) in {str(item) for item in ids if str(item)}


def recipe_path(profile: AppProfile, case_id: str, *, source: str) -> Path:
    """Return the Recipe file a Case resolves to for one source."""
    if source not in RECIPE_SOURCES:
        raise ValueError(f"recipe source must be one of {RECIPE_SOURCES}")
    base = profile.recipes_dir()
    if source == RECIPE_SOURCE_DRAFT:
        base = base / "drafts"
    return base / f"{str(case_id).lower()}.yaml"


def recipe_relative_path(profile: AppProfile, path: Path) -> str | None:
    """App-relative POSIX path, or ``None`` when the file is outside the App."""
    normalized = Path(os.path.normpath(path))
    app_dir = Path(os.path.normpath(profile.profile_dir))
    try:
        relative = normalized.relative_to(app_dir)
    except ValueError:
        return None
    if relative.parts and relative.parts[0] == "..":
        return None
    return relative.as_posix()


def _recorded_path(profile: AppProfile, case_id: str, *, source: str) -> str | None:
    return recipe_relative_path(profile, recipe_path(profile, case_id, source=source))


def _outside_app_path(case_id: str, *, source: str) -> str:
    name = f"{str(case_id).lower()}.yaml"
    if source == RECIPE_SOURCE_DRAFT:
        name = f"drafts/{name}"
    return f"{OUTSIDE_APP_PATH_PREFIX}/{name}"


def load_case_registry(profile: AppProfile) -> dict[str, Any]:
    """Read the Case Registry document for identity resolution, fail-closed."""
    import yaml

    try:
        registry = (
            yaml.safe_load(profile.cases_path().read_text(encoding="utf-8")) or {}
        )
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"draft recipe identity 非法：{exc}") from exc
    if not isinstance(registry, dict):
        raise ContractError("draft recipe identity 非法：Case Registry 不是对象")
    return registry


def case_binding_from_registry(
    registry: dict[str, Any], case_id: str
) -> tuple[int, str | None]:
    """Case revision and reserved external ID from a loaded Registry."""
    from autophone.assets.case_ids import (
        assert_reserved_recipe_binding,
        id_reservations,
    )

    key = str(case_id).upper()
    try:
        assert_reserved_recipe_binding(registry, key)
        reservations = id_reservations(registry)
    except ValueError as exc:
        raise ContractError(f"draft recipe identity 非法：{exc}") from exc
    case = next(
        (
            row
            for row in (registry.get("cases") or [])
            if isinstance(row, dict) and str(row.get("id") or "").upper() == key
        ),
        None,
    )
    if case is None:
        raise ContractError(f"用例 {case_id} 不存在，不能解析 draft recipe identity")
    revision = case.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ContractError(
            f"用例 {case_id} revision 非法，不能解析 draft recipe identity"
        )
    return revision, reservations.get(key)


#: Registries parsed inside ``case_registry_snapshot``; ``None`` outside it.
_REGISTRY_SNAPSHOT: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "case_registry_snapshot", default=None,
)


@contextmanager
def case_registry_snapshot() -> Iterator[None]:
    """Parse each Case Registry at most once within one read-only pass.

    Only ``current_case_binding`` consults the snapshot, and only in the
    current context; a registry that fails to load is not cached.  Outside the
    block every call re-reads the file as before.
    """
    token = _REGISTRY_SNAPSHOT.set({})
    try:
        yield
    finally:
        _REGISTRY_SNAPSHOT.reset(token)


def current_case_binding(profile: AppProfile, case_id: str) -> tuple[int, str | None]:
    """Current Case revision and reserved external ID, fail-closed."""
    snapshot = _REGISTRY_SNAPSHOT.get()
    if snapshot is None:
        return case_binding_from_registry(load_case_registry(profile), case_id)
    key = str(profile.cases_path())
    if key not in snapshot:
        snapshot[key] = load_case_registry(profile)
    return case_binding_from_registry(snapshot[key], case_id)


def current_draft_recipe_identity(
    profile: AppProfile,
    case_id: str,
) -> dict[str, Any] | None:
    """Return the current Case + reserved identity + draft file digest."""
    revision, reserved = current_case_binding(profile, case_id)
    path = recipe_path(profile, case_id, source=RECIPE_SOURCE_DRAFT)
    if not path.is_file():
        return None
    try:
        recipe_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ContractError(f"读取 draft recipe {path} 失败：{exc}") from exc
    return {
        "case_revision": revision,
        "reserved_external_id": reserved,
        "draft_recipe_sha256": recipe_sha256,
    }


def executed_recipe_identity(
    profile: AppProfile,
    case_id: str,
    *,
    source: str,
    recipe_sha256: str | None,
    case_revision: int,
    reserved_external_id: str | None,
    unverified_reason: str | None = None,
) -> dict[str, Any]:
    """Build the executed Recipe identity record stored on a Case result.

    A Recipe directory outside the App directory is recorded as unverified
    (``recipe_path_outside_app``) with a placeholder path, never a host path.
    """
    path = _recorded_path(profile, case_id, source=source)
    if path is None:
        path = _outside_app_path(case_id, source=source)
        recipe_sha256 = None
        unverified_reason = unverified_reason or REASON_PATH_OUTSIDE_APP
    identity: dict[str, Any] = {
        "source": source,
        "path": path,
        "recipe_sha256": recipe_sha256,
        "case_revision": case_revision,
        "reserved_external_id": reserved_external_id,
    }
    if unverified_reason is not None:
        identity["unverified_reason"] = unverified_reason
    return identity


def current_recipe_identity(
    profile: AppProfile,
    case_id: str,
    *,
    source: str,
) -> dict[str, Any] | None:
    """Return the current executed-Recipe identity for one source.

    ``None`` means the selected Recipe file does not exist.  A Case binding
    that cannot be resolved raises ``ContractError``; so does a live Recipe
    outside the App directory, whose identity a Run records as unverified.
    A draft identity outside the App keeps its digest because the draft rule
    compares only revision, reserved ID and digest.
    """
    revision, reserved = current_case_binding(profile, case_id)
    path = recipe_path(profile, case_id, source=source)
    relative = recipe_relative_path(profile, path)
    if relative is None and source == RECIPE_SOURCE_LIVE:
        raise ContractError(f"用例 {case_id} 的 live recipe 不在 App 目录内")
    if not path.is_file():
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ContractError(f"读取 recipe {path} 失败：{exc}") from exc
    return {
        "source": source,
        "path": relative or _outside_app_path(case_id, source=source),
        "recipe_sha256": digest,
        "case_revision": revision,
        "reserved_external_id": reserved,
    }


def trial_matches_identity(
    run_case: dict[str, Any],
    current_identity: dict[str, Any] | None,
) -> bool:
    """Draft Trial evidence rule used by the Promotion Gate."""
    identity = run_case.get("draft_recipe_identity")
    revision = run_case.get("revision")
    return bool(
        current_identity is not None
        and run_case.get("exec_via") == "recipe"
        and isinstance(identity, dict)
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision == identity.get("case_revision")
        and identity == current_identity
    )


def recorded_recipe_identity(run_case: dict[str, Any]) -> dict[str, Any] | None:
    """Return a Case result's verified executed-Recipe identity, if any."""
    identity = run_case.get("recipe_identity")
    if (
        not isinstance(identity, dict)
        or identity.get("source") not in RECIPE_SOURCES
        or not isinstance(identity.get("recipe_sha256"), str)
        or "unverified_reason" in identity
    ):
        return None
    return identity


def run_matches_recipe_identity(
    run_case: dict[str, Any],
    current_identity: dict[str, Any] | None,
) -> bool:
    """Whether a Case result was produced by exactly the current Recipe identity.

    ``current_identity`` is the executed-Recipe form from
    ``current_recipe_identity``.  A draft identity is judged by the draft
    Trial rule on ``draft_recipe_identity``; a live identity requires a
    verified ``recipe_identity`` equal to the current one.
    """
    if current_identity is None:
        return False
    source = current_identity.get("source")
    if source == RECIPE_SOURCE_DRAFT:
        return trial_matches_identity(
            run_case,
            {
                "case_revision": current_identity.get("case_revision"),
                "reserved_external_id": current_identity.get("reserved_external_id"),
                "draft_recipe_sha256": current_identity.get("recipe_sha256"),
            },
        )
    if source != RECIPE_SOURCE_LIVE:
        return False
    identity = recorded_recipe_identity(run_case)
    revision = run_case.get("revision")
    if not (
        run_case.get("exec_via") == "recipe"
        and identity is not None
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision == identity.get("case_revision")
    ):
        return False
    core_keys = ("source", "path", "recipe_sha256", "case_revision", "reserved_external_id")
    if any(identity.get(key) != current_identity.get(key) for key in core_keys):
        return False
    if "binding_sha256" in current_identity or identity.get("binding_sha256") is not None:
        if identity.get("binding_sha256") != current_identity.get("binding_sha256"):
            return False
    return True


def artifact_content_digest_from_manifest(
    app: str,
    *,
    state_root: Path,
    build_label: str | None = None,
    build_id: str | None = None,
) -> str | None:
    """Resolve the current exact artifact SHA-256 from ``packages/<app>/manifest.json``.

    Pure JSON reader (never imports ``autophone.packages`` so ``assets`` stays acyclic).
    Supports both v2 and legacy v1 manifests.  Returns ``None`` when no manifest
    exists or no unambiguous current build can be resolved.
    """
    manifest_path = Path(state_root) / "packages" / str(app) / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, Mapping):
        return None

    schema_version = str(doc.get("schema_version") or ("v2" if "builds" in doc else "v1"))
    if schema_version == "v2":
        builds = [row for row in (doc.get("builds") or []) if isinstance(row, Mapping)]
        slots = {
            str(slot.get("slot") or ""): slot
            for slot in (doc.get("version_slots") or [])
            if isinstance(slot, Mapping) and slot.get("slot")
        }

        def _build_sha(row: Mapping[str, Any] | None) -> str | None:
            if not isinstance(row, Mapping):
                return None
            artifact = row.get("artifact")
            if isinstance(artifact, Mapping):
                return normalize_sha256(artifact.get("sha256"))
            return normalize_sha256(row.get("sha256"))

        if build_id:
            target = next((row for row in builds if str(row.get("build_id") or "") == str(build_id)), None)
            return _build_sha(target)

        parsed = _LABEL_BUILD_RE.match(build_label or "")
        if parsed is not None:
            matches = []
            for row in builds:
                row_bid = str(row.get("build_id") or "")
                if not row_bid.startswith(parsed["build"]):
                    continue
                slot_row = slots.get(str(row.get("slot") or ""))
                short_ver = slot_row.get("short_version") if isinstance(slot_row, Mapping) else None
                if short_ver is not None and str(short_ver) != parsed["short"]:
                    continue
                matches.append(row)
            return _build_sha(matches[0]) if len(matches) == 1 else None

        default_slot = doc.get("default_slot") or doc.get("active_slot")
        if default_slot:
            slot_row = slots.get(str(default_slot))
            candidate_id = slot_row.get("candidate_build_id") if isinstance(slot_row, Mapping) else None
            if not candidate_id:
                return None
            target = next((row for row in builds if row.get("build_id") == candidate_id), None)
            return _build_sha(target)

        active_slots = [slot for slot in slots.values() if not slot.get("archived")]
        if not active_slots:
            return None
        latest = max(active_slots, key=lambda slot: str(slot.get("slot") or ""))
        candidate_id = latest.get("candidate_build_id")
        if not candidate_id:
            return None
        target = next((row for row in builds if row.get("build_id") == candidate_id), None)
        return _build_sha(target)

    if schema_version == "v1":
        entries = [row for row in (doc.get("packages") or []) if isinstance(row, Mapping)]
        if not entries:
            return None
        default_slot = doc.get("default_slot") or doc.get("active_slot")
        if default_slot:
            target = next((row for row in entries if str(row.get("slot") or "") == str(default_slot)), None)
            return normalize_sha256(target.get("sha256")) if isinstance(target, Mapping) else None
        latest = max(entries, key=lambda row: str(row.get("slot") or ""))
        return normalize_sha256(latest.get("sha256"))

    return None


def current_artifact_content_digest(
    profile: AppProfile | None,
    *,
    build_id: str | None = None,
    state_root: Path | None = None,
) -> str | None:
    """Resolve the current target build's exact artifact content digest for ``profile``."""
    if profile is None:
        return None
    pkg_root = (
        state_root
        or getattr(profile, "host_state_root", None)
        or getattr(profile, "state_root", None)
        or getattr(profile, "root", None)
    )
    if pkg_root is None:
        return None
    return artifact_content_digest_from_manifest(
        str(profile.app),
        state_root=Path(pkg_root),
        build_label=getattr(profile, "build_label", None),
        build_id=build_id,
    )


def _operations_index_from_path(operations_path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if operations_path is None or not operations_path.is_file():
        return {}
    import yaml

    try:
        doc = yaml.safe_load(operations_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(doc, Mapping):
        return {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in doc.get("operations") or []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(dict(row))
    return by_name


def steps_use_symbol_binding(
    steps: Iterable[Any] | None,
    *,
    operations_by_name: Mapping[str, Any] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> bool:
    """Return True if any Recipe/Operation step (directly or via Operation) uses symbol binding."""
    if not steps:
        return False
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        raw_symbol = step.get("symbol")
        if isinstance(raw_symbol, str) and raw_symbol.strip():
            return True
        if isinstance(step.get("_symbol_binding"), Mapping):
            return True
        op_name = step.get("operation")
        if isinstance(op_name, str) and op_name and operations_by_name and op_name not in _seen:
            raw_ops = operations_by_name.get(op_name)
            candidates: list[Mapping[str, Any]] = []
            if isinstance(raw_ops, Mapping):
                candidates = [raw_ops]
            elif isinstance(raw_ops, list):
                active = [item for item in raw_ops if isinstance(item, Mapping) and item.get("status") == "active"]
                candidates = active or [item for item in raw_ops if isinstance(item, Mapping)]
            for op_doc in candidates:
                if steps_use_symbol_binding(
                    op_doc.get("steps"),
                    operations_by_name=operations_by_name,
                    _seen=_seen | {op_name},
                ):
                    return True
    return False


def _load_recipe_steps_for_case(
    profile: AppProfile,
    case_id: str,
    *,
    recipe_source: str | None = None,
) -> list[Any]:
    import yaml

    if not hasattr(profile, "recipes_dir"):
        return []
    if recipe_source == RECIPE_SOURCE_DRAFT:
        paths = (recipe_path(profile, case_id, source=RECIPE_SOURCE_DRAFT),)
    elif recipe_source == RECIPE_SOURCE_LIVE:
        paths = (recipe_path(profile, case_id, source=RECIPE_SOURCE_LIVE),)
    else:
        paths = (
            recipe_path(profile, case_id, source=RECIPE_SOURCE_LIVE),
            recipe_path(profile, case_id, source=RECIPE_SOURCE_DRAFT),
        )
    for path in paths:
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        steps = doc.get("steps") if isinstance(doc, Mapping) else None
        return steps if isinstance(steps, list) else []
    return []


def current_symbol_binding_sha256(
    profile_or_app_dir: AppProfile | Path | None,
    *,
    app: str | None = None,
    artifact_content_digest: str | None,
) -> str | None:
    """Resolve the current immutable ``binding_sha256`` from ``symbols/artifacts/*.yaml``.

    Performs a single-read raw-byte SHA-256 per file, validates against
    ``app-symbols.v1.json``, and matches exact canonical ``artifact_content_digest``.
    Returns ``None`` (fail-closed) when missing, invalid, or ambiguous.
    """
    expected = normalize_sha256(artifact_content_digest)
    if profile_or_app_dir is None or expected is None:
        return None
    if isinstance(profile_or_app_dir, Path):
        app_dir = profile_or_app_dir
        expected_app = app
    else:
        raw_dir = getattr(profile_or_app_dir, "profile_dir", None)
        if raw_dir is None:
            return None
        app_dir = Path(raw_dir)
        expected_app = app or str(getattr(profile_or_app_dir, "app", "") or "") or None
    artifacts_dir = app_dir / "symbols" / "artifacts"
    if not artifacts_dir.is_dir():
        return None

    import yaml

    matches: list[str] = []
    for path in sorted(artifacts_dir.glob("*.yaml")):
        if not path.is_file():
            continue
        try:
            raw_bytes = path.read_bytes()
            doc = yaml.safe_load(raw_bytes)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, Mapping):
            continue
        if doc.get("schema") != _APP_SYMBOLS_SCHEMA:
            continue
        if expected_app is not None and str(doc.get("app") or "") != expected_app:
            continue
        doc_digest = canonical_artifact_content_digest(doc.get("artifact_identity"))
        if doc_digest != expected:
            continue
        try:
            validate_or_raise("app-symbols.v1.json", dict(doc))
        except ContractError:
            return None
        matches.append(hashlib.sha256(raw_bytes).hexdigest())
    return matches[0] if len(matches) == 1 else None


def current_discrimination_context(
    profile: AppProfile | None,
    *,
    case_id: str | None = None,
    recipe_source: str | None = None,
    steps: Iterable[Any] | None = None,
    op_def: Mapping[str, Any] | None = None,
    current_artifact_content_digest: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool | None = None,
    build_id: str | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble exact artifact content digest & symbol binding freshness context (D6)."""
    if current_artifact_content_digest is not None:
        resolved_digest: str | None = current_artifact_content_digest
    elif profile is not None:
        resolved_digest = globals()["current_artifact_content_digest"](
            profile, build_id=build_id, state_root=state_root,
        )
    else:
        resolved_digest = None

    if uses_symbol_binding is not None:
        resolved_uses_symbols = bool(uses_symbol_binding)
    elif current_binding_sha256 is not None:
        resolved_uses_symbols = True
    else:
        ops_path = (
            profile.operations_path()
            if profile is not None and callable(getattr(profile, "operations_path", None))
            else None
        )
        ops_index = _operations_index_from_path(ops_path)
        if steps is not None:
            inspect_steps: Iterable[Any] = steps
        elif isinstance(op_def, Mapping):
            inspect_steps = op_def.get("steps") or []
        elif profile is not None and case_id:
            inspect_steps = _load_recipe_steps_for_case(
                profile, case_id, recipe_source=recipe_source,
            )
        else:
            inspect_steps = []
        resolved_uses_symbols = steps_use_symbol_binding(
            inspect_steps, operations_by_name=ops_index,
        )

    if current_binding_sha256 is not None:
        resolved_binding: str | None = current_binding_sha256
    elif resolved_uses_symbols and profile is not None:
        resolved_binding = current_symbol_binding_sha256(
            profile, artifact_content_digest=resolved_digest,
        )
    else:
        resolved_binding = None

    return {
        "current_artifact_content_digest": resolved_digest,
        "current_binding_sha256": resolved_binding,
        "uses_symbol_binding": resolved_uses_symbols,
    }
