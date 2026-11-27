"""Unified immutable symbol binding loader, artifact digest routing, and external claims store.

Implements Decisions D2, D5, and D8 for ``app-symbols.v1``:
- Pure-content ``artifact_content_digest`` (strictly excluding ``target_fingerprint``)
  for both ``ipa_build`` and ``rn_candidate`` artifacts.
- Exact multi-version routing under ``apps/<app>/symbols/artifacts/`` with single-read
  ``binding_sha256`` computation, schema validation against ``app-symbols.v1.json``,
  and fail-closed handling for missing, ambiguous, or tampered bindings.
- External symbol claims store at ``apps/<app>/symbols/claims.json`` conforming to
  ``knowledge-claims.v1.json`` so runtime ``symbol.unique_visibility`` observations
  never mutate immutable symbol YAML files or invalidate ``binding_sha256``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence, cast, runtime_checkable

import yaml

from autophone.assets._package_manifest import (
    PackageBuildInfo,
    PackageManifestError,
    resolve_package_manifest_build,
)
from autophone.assets.apps import AppProfile, VALID_AX_BACKENDS
from autophone.assets.build_identity import load_document as load_build_identity_document
from autophone.assets.store import path_write_lock
from autophone.contracts import ContractError, validate_or_raise
from autophone.paths import STATE_ROOT
from autophone.util import read_json, write_json_atomic

APP_SYMBOLS_SCHEMA = "autophone.app-symbols/1"
APP_SYMBOLS_SCHEMA_FILE = "app-symbols.v1.json"
KNOWLEDGE_CLAIMS_SCHEMA_FILE = "knowledge-claims.v1.json"

CLAIM_KIND_LOCATOR_BINDING = "symbol.locator_binding"
CLAIM_KIND_UNIQUE_VISIBILITY = "symbol.unique_visibility"
CLAIM_KIND_EFFECT_SET = "symbol.effect_set"

_OBSERVED_EVIDENCE_LEVELS = frozenset({
    "observed",
    "repeated",
    "controlled_contrast",
    "independent_readback",
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_STEM_RE = re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{64})$")

ArtifactKind = Literal["ipa_build", "rn_candidate"]
EffectCompleteness = Literal["exhaustive", "lower_bound", "unknown"]
LocatorKind = Literal["ax_identifier"]
ArtifactResolutionSource = Literal[
    "build_identity_confirmed",
    "explicit_build_id",
    "package_snapshot",
    "package_store_default",
]


# ── Exceptions ────────────────────────────────────────────────────────────────


class SymbolBindingError(ValueError):
    """Base fail-closed error for ``app-symbols.v1`` loading, routing, and claims."""

    code: str = "symbol_binding_error"


class SymbolBindingMissingError(SymbolBindingError):
    """Raised when no symbol binding file matches the requested artifact digest."""

    code: str = "symbol_binding_missing"


class SymbolBindingAmbiguousError(SymbolBindingError):
    """Raised when multiple symbol binding files match the same artifact digest."""

    code: str = "symbol_binding_ambiguous"


class SymbolBindingMismatchError(SymbolBindingError):
    """Raised on app, digest, build_id, filename, or identity-state mismatch."""

    code: str = "symbol_binding_mismatch"


class SymbolClaimError(SymbolBindingError):
    """Raised when reading or writing external symbol claims violates invariants."""

    code: str = "symbol_claim_error"


# ── Immutable Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolScopeSpec:
    """Normalized page scope of a logical control symbol."""

    screen_id: str
    region: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class SymbolLocatorSpec:
    """Constrained AX locator declared for a symbol in ``app-symbols.v1``."""

    kind: LocatorKind
    identifier: str
    role: str | None = None
    enabled_only: bool = True
    require_unique: bool = True


@dataclass(frozen=True)
class SymbolEffectSpec:
    """Multi-effect verb set and completeness posture for a control symbol."""

    verbs: frozenset[str]
    completeness: EffectCompleteness


@dataclass(frozen=True)
class SymbolControlSpec:
    """Immutable view of one entry in ``app-symbols.v1`` ``controls``."""

    symbol_id: str
    kind: str
    screen_id: str
    region: tuple[float, float, float, float] | None
    locator: SymbolLocatorSpec
    effects: SymbolEffectSpec

    @property
    def scope(self) -> SymbolScopeSpec:
        return SymbolScopeSpec(screen_id=self.screen_id, region=self.region)


@dataclass(frozen=True)
class LoadedSymbolBinding:
    """Frozen, schema-validated binding loaded from one single byte read."""

    app: str
    path: Path
    binding_sha256: str
    artifact_content_digest: str
    artifact_kind: ArtifactKind
    artifact_identity: Mapping[str, Any]
    controls: Mapping[str, SymbolControlSpec]
    build_id: str | None = None
    bundle_id: str = ""
    short_version: str = ""
    build_version: str = ""

    def get_control(self, symbol_id: str) -> SymbolControlSpec | None:
        return self.controls.get(symbol_id)

    def require_control(self, symbol_id: str) -> SymbolControlSpec:
        control = self.controls.get(symbol_id)
        if control is None:
            raise SymbolBindingMissingError(
                f"symbol {symbol_id!r} not found in {self.path.name} "
                f"(digest={self.artifact_content_digest[:12]})"
            )
        return control


@dataclass(frozen=True)
class ResolvedArtifactIdentity:
    """Pure-content artifact identity resolved for symbol routing (no ``target_fingerprint``)."""

    app: str
    kind: ArtifactKind
    artifact_content_digest: str
    source: ArtifactResolutionSource
    build_id: str | None = None
    bundle_id: str | None = None
    short_version: str | None = None
    build_version: str | None = None
    artifact_sha256: str | None = None
    base_ipa_sha256: str | None = None
    main_jsbundle_sha256: str | None = None
    assets_tree_sha256: str | None = None
    identity_state: str | None = None

    def to_artifact_identity(self) -> dict[str, Any]:
        """Return a pure-content ``artifact_identity`` mapping without ``target_fingerprint``."""
        base: dict[str, Any] = {
            "kind": self.kind,
            "bundle_id": self.bundle_id or "",
            "short_version": self.short_version or "",
            "build_version": self.build_version or "",
        }
        if self.kind == "ipa_build":
            if self.build_id:
                base["build_id"] = self.build_id
            if self.artifact_sha256:
                base["artifact_sha256"] = self.artifact_sha256
        else:
            if self.base_ipa_sha256:
                base["base_ipa_sha256"] = self.base_ipa_sha256
            if self.main_jsbundle_sha256:
                base["main_jsbundle_sha256"] = self.main_jsbundle_sha256
            if self.assets_tree_sha256:
                base["assets_tree_sha256"] = self.assets_tree_sha256
        return base


# ── Path Helpers ──────────────────────────────────────────────────────────────


def _resolve_app_dir(app_dir_or_profile: Path | AppProfile) -> tuple[Path, str | None]:
    if isinstance(app_dir_or_profile, AppProfile):
        return app_dir_or_profile.profile_dir, app_dir_or_profile.app
    return Path(app_dir_or_profile), None


def symbols_dir(app_dir_or_profile: Path | AppProfile) -> Path:
    app_dir, _ = _resolve_app_dir(app_dir_or_profile)
    return app_dir / "symbols"


def symbols_artifacts_dir(app_dir_or_profile: Path | AppProfile) -> Path:
    return symbols_dir(app_dir_or_profile) / "artifacts"


def symbol_claims_path(app_dir_or_profile: Path | AppProfile) -> Path:
    return symbols_dir(app_dir_or_profile) / "claims.json"


def artifact_filename_for_digest(artifact_content_digest: str, *, full: bool = False) -> str:
    digest = _normalize_sha256(artifact_content_digest, "artifact_content_digest")
    return f"{digest}.yaml" if full else f"{digest[:12]}.yaml"


# ── Canonical Pure-Content Digest ─────────────────────────────────────────────


def _normalize_sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SymbolBindingError(f"{field_name} must be a 64-hex sha256 string, got {value!r}")
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise SymbolBindingError(f"{field_name} must be a 64-hex sha256 string, got {value!r}")
    return normalized


def canonical_ipa_content_digest(artifact_sha256: str) -> str:
    """Canonical pure-content digest for an ``ipa_build`` artifact."""
    return _normalize_sha256(artifact_sha256, "artifact_sha256")


def canonical_rn_candidate_content_digest(
    *,
    base_ipa_sha256: str,
    main_jsbundle_sha256: str,
    assets_tree_sha256: str,
) -> str:
    """Canonical pure-content digest for an ``rn_candidate`` triplet.

    Strictly excludes Rig-specific ``target_fingerprint`` and all install metadata.
    Shared exact canonical contract with M5 (``negative_control.py`` / ``promotion.py``).
    """
    payload = {
        "kind": "rn_candidate",
        "base_ipa_sha256": _normalize_sha256(base_ipa_sha256, "base_ipa_sha256"),
        "main_jsbundle_sha256": _normalize_sha256(main_jsbundle_sha256, "main_jsbundle_sha256"),
        "assets_tree_sha256": _normalize_sha256(assets_tree_sha256, "assets_tree_sha256"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def canonical_artifact_content_digest(artifact_identity: Mapping[str, Any]) -> str:
    """Compute the pure-content ``artifact_content_digest`` for ``ipa_build`` or ``rn_candidate``.

    Accepts:
    - ``app-symbols.v1`` ``artifact_identity`` dicts (``kind: ipa_build | rn_candidate``)
    - ``build-identity.v2.json`` ``candidate_identity`` dicts (strictly stripping ``target_fingerprint``)
    - Package Store ``RegistryBuildRef`` dicts containing ``artifact_sha256``
    """
    if not isinstance(artifact_identity, Mapping):
        raise SymbolBindingError(
            f"artifact_identity must be a mapping, got {type(artifact_identity).__name__}"
        )

    raw_kind = artifact_identity.get("kind")
    rn_keys = ("base_ipa_sha256", "main_jsbundle_sha256", "assets_tree_sha256")
    has_rn_triplet = all(artifact_identity.get(k) not in (None, "") for k in rn_keys)
    has_any_rn_key = any(k in artifact_identity for k in rn_keys)
    has_ipa_sha = artifact_identity.get("artifact_sha256") not in (None, "")

    if raw_kind == "ipa_build":
        if has_any_rn_key:
            raise SymbolBindingError("ipa_build artifact_identity must not include rn_candidate hashes")
        return canonical_ipa_content_digest(artifact_identity.get("artifact_sha256"))  # type: ignore[arg-type]

    if raw_kind == "rn_candidate":
        if "artifact_sha256" in artifact_identity:
            raise SymbolBindingError("rn_candidate artifact_identity must not include artifact_sha256")
        return canonical_rn_candidate_content_digest(
            base_ipa_sha256=artifact_identity.get("base_ipa_sha256"),  # type: ignore[arg-type]
            main_jsbundle_sha256=artifact_identity.get("main_jsbundle_sha256"),  # type: ignore[arg-type]
            assets_tree_sha256=artifact_identity.get("assets_tree_sha256"),  # type: ignore[arg-type]
        )

    if raw_kind is not None:
        raise SymbolBindingError(f"unsupported artifact_identity kind: {raw_kind!r}")

    # Untyped dicts (e.g. build-identity.v2 candidate_identity or RegistryBuildRef dict)
    if has_rn_triplet and has_ipa_sha:
        raise SymbolBindingError(
            "ambiguous artifact_identity: contains both artifact_sha256 and rn_candidate triplet"
        )
    if has_rn_triplet or artifact_identity.get("candidate_kind") in {"rn_update", "embedded"}:
        return canonical_rn_candidate_content_digest(
            base_ipa_sha256=artifact_identity.get("base_ipa_sha256"),  # type: ignore[arg-type]
            main_jsbundle_sha256=artifact_identity.get("main_jsbundle_sha256"),  # type: ignore[arg-type]
            assets_tree_sha256=artifact_identity.get("assets_tree_sha256"),  # type: ignore[arg-type]
        )
    if has_ipa_sha:
        return canonical_ipa_content_digest(artifact_identity.get("artifact_sha256"))  # type: ignore[arg-type]

    raise SymbolBindingError(
        "artifact_identity lacks required artifact_sha256 or rn_candidate hash triplet"
    )


@runtime_checkable
class BuildRefLike(Protocol):
    """Minimal protocol satisfied by Package Store ``RegistryBuildRef`` objects."""

    def to_dict(self) -> Mapping[str, Any]: ...


def artifact_identity_from_build_ref(
    build_ref: BuildRefLike | Mapping[str, Any],
    *,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    """Build a pure-content ``ipa_build`` ``artifact_identity`` dict from a Package Store ref."""
    if isinstance(build_ref, Mapping):
        raw = dict(build_ref)
    elif callable(getattr(build_ref, "to_dict", None)):
        raw = dict(build_ref.to_dict())
    else:
        raise SymbolBindingError(
            f"build_ref must be a mapping or expose to_dict(), got {type(build_ref).__name__}"
        )
    sha = canonical_ipa_content_digest(raw.get("artifact_sha256"))  # type: ignore[arg-type]
    build_id = str(raw.get("build_id") or "").strip()
    if not build_id:
        raise SymbolBindingError("build_ref is missing build_id")
    return {
        "kind": "ipa_build",
        "bundle_id": str(bundle_id or raw.get("bundle_id") or ""),
        "short_version": str(raw.get("short_version") or ""),
        "build_version": str(raw.get("build_version") or ""),
        "build_id": build_id,
        "artifact_sha256": sha,
    }


def artifact_identity_from_rn_candidate(
    candidate_identity: Mapping[str, Any],
    *,
    short_version: str = "",
    build_version: str = "",
) -> dict[str, Any]:
    """Strip ``target_fingerprint`` and rig metadata from a ``build-identity.v2`` candidate."""
    if not isinstance(candidate_identity, Mapping):
        raise SymbolBindingError("candidate_identity must be a mapping")
    return {
        "kind": "rn_candidate",
        "bundle_id": str(candidate_identity.get("bundle_id") or ""),
        "short_version": str(short_version or ""),
        "build_version": str(build_version or ""),
        "base_ipa_sha256": _normalize_sha256(
            candidate_identity.get("base_ipa_sha256"), "base_ipa_sha256"
        ),
        "main_jsbundle_sha256": _normalize_sha256(
            candidate_identity.get("main_jsbundle_sha256"), "main_jsbundle_sha256"
        ),
        "assets_tree_sha256": _normalize_sha256(
            candidate_identity.get("assets_tree_sha256"), "assets_tree_sha256"
        ),
    }


# ── Single-Read Loader & Multi-Version Routing ────────────────────────────────


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(copy.deepcopy(dict(value)))


def _parse_control(symbol_id: str, raw: Mapping[str, Any]) -> SymbolControlSpec:
    scope = raw["scope"]
    raw_region = scope.get("region")
    region: tuple[float, float, float, float] | None = None
    if raw_region is not None:
        region = (
            float(raw_region[0]),
            float(raw_region[1]),
            float(raw_region[2]),
            float(raw_region[3]),
        )
    loc = raw["locator"]
    raw_role = loc.get("role")
    locator = SymbolLocatorSpec(
        kind=cast(LocatorKind, str(loc["kind"])),
        identifier=str(loc["identifier"]),
        role=str(raw_role) if raw_role is not None else None,
        enabled_only=bool(loc["enabled_only"]),
        require_unique=bool(loc["require_unique"]),
    )
    eff = raw["effects"]
    effects = SymbolEffectSpec(
        verbs=frozenset(str(v) for v in eff["verbs"]),
        completeness=cast(EffectCompleteness, str(eff["completeness"])),
    )
    return SymbolControlSpec(
        symbol_id=symbol_id,
        kind=str(raw["kind"]),
        screen_id=str(scope["screen_id"]),
        region=region,
        locator=locator,
        effects=effects,
    )


def _parse_yaml_bytes(raw_bytes: bytes, path: Path) -> Any:
    try:
        text = raw_bytes.decode("utf-8")
        return yaml.safe_load(text)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SymbolBindingError(f"failed to parse symbol binding YAML {path.name}: {exc}") from exc


def _build_loaded_binding(
    path: Path,
    raw_bytes: bytes,
    doc: Any,
    *,
    expected_app: str | None = None,
    expected_artifact_content_digest: str | None = None,
    expected_build_id: str | None = None,
) -> LoadedSymbolBinding:
    if not isinstance(doc, dict):
        raise SymbolBindingError(f"symbol binding {path.name} must be a YAML mapping")
    try:
        validate_or_raise(APP_SYMBOLS_SCHEMA_FILE, doc)
    except ContractError as exc:
        raise SymbolBindingError(f"symbol binding {path.name} failed schema validation: {exc}") from exc

    doc_app = str(doc["app"])
    if expected_app is not None and doc_app != expected_app:
        raise SymbolBindingMismatchError(
            f"symbol binding {path.name} app {doc_app!r} does not match expected app {expected_app!r}"
        )

    artifact_identity = doc["artifact_identity"]
    internal_digest = canonical_artifact_content_digest(artifact_identity)
    if expected_artifact_content_digest is not None:
        expected_digest = _normalize_sha256(
            expected_artifact_content_digest, "expected_artifact_content_digest"
        )
        if internal_digest != expected_digest:
            raise SymbolBindingMismatchError(
                f"symbol binding {path.name} internal artifact_content_digest "
                f"{internal_digest} does not match expected {expected_digest}"
            )

    artifact_kind = cast(ArtifactKind, str(artifact_identity["kind"]))
    doc_build_id = (
        str(artifact_identity["build_id"]) if artifact_kind == "ipa_build" else None
    )
    if expected_build_id is not None:
        if artifact_kind != "ipa_build" or doc_build_id != expected_build_id:
            raise SymbolBindingMismatchError(
                f"symbol binding {path.name} build_id {doc_build_id!r} "
                f"does not match expected build_id {expected_build_id!r}"
            )

    controls = MappingProxyType({
        str(symbol_id): _parse_control(str(symbol_id), raw_control)
        for symbol_id, raw_control in doc["controls"].items()
    })
    binding_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return LoadedSymbolBinding(
        app=doc_app,
        path=path,
        binding_sha256=binding_sha256,
        artifact_content_digest=internal_digest,
        artifact_kind=artifact_kind,
        artifact_identity=_freeze_mapping(artifact_identity),
        controls=controls,
        build_id=doc_build_id,
        bundle_id=str(artifact_identity["bundle_id"]),
        short_version=str(artifact_identity["short_version"]),
        build_version=str(artifact_identity["build_version"]),
    )


def load_symbol_binding_file(
    path: str | Path,
    *,
    expected_app: str | None = None,
    expected_artifact_content_digest: str | None = None,
    expected_build_id: str | None = None,
) -> LoadedSymbolBinding:
    """Single-read, hash, schema-validate, and verify one ``app-symbols.v1`` file."""
    target = Path(path)
    if target.is_symlink():
        raise SymbolBindingError(f"symbol binding path must not be a symlink: {target}")
    if not target.is_file():
        raise SymbolBindingMissingError(f"symbol binding file not found: {target}")
    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise SymbolBindingError(f"unable to read symbol binding {target}: {exc}") from exc
    doc = _parse_yaml_bytes(raw_bytes, target)
    return _build_loaded_binding(
        target,
        raw_bytes,
        doc,
        expected_app=expected_app,
        expected_artifact_content_digest=expected_artifact_content_digest,
        expected_build_id=expected_build_id,
    )


def load_symbol_binding(
    app_dir_or_profile: Path | AppProfile,
    *,
    artifact_content_digest: str | None = None,
    artifact_identity: Mapping[str, Any] | None = None,
    expected_app: str | None = None,
    expected_build_id: str | None = None,
) -> LoadedSymbolBinding:
    """Route and load the unique symbol binding under ``apps/<app>/symbols/artifacts/``.

    Matches ``<digest[:12]>.yaml``, ``<digest>.yaml``, or any ``*.yaml`` in
    ``symbols/artifacts/`` whose ``canonical_artifact_content_digest(doc["artifact_identity"])``
    equals the requested digest. Every candidate file is read once into bytes to compute
    ``binding_sha256``. Fails closed on missing files, multiple files matching the same
    digest, schema errors, or tampered/mismatched internal digests.
    """
    app_dir, profile_app = _resolve_app_dir(app_dir_or_profile)
    resolved_app = expected_app if expected_app is not None else profile_app

    digest_from_identity = (
        canonical_artifact_content_digest(artifact_identity)
        if artifact_identity is not None
        else None
    )
    digest_from_arg = (
        _normalize_sha256(artifact_content_digest, "artifact_content_digest")
        if artifact_content_digest is not None
        else None
    )
    if digest_from_identity is not None and digest_from_arg is not None:
        if digest_from_identity != digest_from_arg:
            raise SymbolBindingMismatchError(
                f"artifact_content_digest {digest_from_arg} disagrees with "
                f"artifact_identity digest {digest_from_identity}"
            )
    expected_digest = digest_from_arg or digest_from_identity
    if expected_digest is None:
        raise SymbolBindingError(
            "either artifact_content_digest or artifact_identity must be provided"
        )

    if (
        expected_build_id is None
        and isinstance(artifact_identity, Mapping)
        and artifact_identity.get("kind") == "ipa_build"
        and artifact_identity.get("build_id") not in (None, "")
    ):
        expected_build_id = str(artifact_identity["build_id"])

    artifacts_dir = symbols_artifacts_dir(app_dir)
    if not artifacts_dir.is_dir():
        raise SymbolBindingMissingError(
            f"symbols artifacts directory not found: {artifacts_dir} "
            f"(requested digest={expected_digest[:12]})"
        )
    artifacts_root = artifacts_dir.resolve()
    yaml_files = sorted(artifacts_dir.glob("*.yaml"))
    if not yaml_files:
        raise SymbolBindingMissingError(
            f"no symbol binding files under {artifacts_dir} "
            f"(requested digest={expected_digest[:12]})"
        )

    named_stems = {expected_digest, expected_digest[:12]}
    matches: list[LoadedSymbolBinding] = []

    for candidate in yaml_files:
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(artifacts_root):
            raise SymbolBindingError(f"unsafe symbol binding path: {candidate}")
        try:
            raw_bytes = candidate.read_bytes()
        except OSError as exc:
            raise SymbolBindingError(f"unable to read symbol binding {candidate}: {exc}") from exc

        stem = candidate.stem.lower()
        is_named_match = stem in named_stems
        doc = _parse_yaml_bytes(raw_bytes, candidate)

        if is_named_match:
            matches.append(
                _build_loaded_binding(
                    candidate,
                    raw_bytes,
                    doc,
                    expected_app=resolved_app,
                    expected_artifact_content_digest=expected_digest,
                    expected_build_id=expected_build_id,
                )
            )
            continue

        if not isinstance(doc, dict) or not isinstance(doc.get("artifact_identity"), Mapping):
            raise SymbolBindingError(
                f"invalid symbol binding document in {artifacts_dir}: {candidate.name}"
            )
        internal_digest = canonical_artifact_content_digest(doc["artifact_identity"])
        if internal_digest != expected_digest:
            continue

        if _DIGEST_STEM_RE.fullmatch(stem) and not expected_digest.startswith(stem):
            raise SymbolBindingMismatchError(
                f"symbol binding {candidate.name} filename stem {stem!r} contradicts "
                f"internal artifact_content_digest {internal_digest}"
            )
        matches.append(
            _build_loaded_binding(
                candidate,
                raw_bytes,
                doc,
                expected_app=resolved_app,
                expected_artifact_content_digest=expected_digest,
                expected_build_id=expected_build_id,
            )
        )

    if not matches:
        raise SymbolBindingMissingError(
            f"no symbol binding matching artifact_content_digest {expected_digest} "
            f"under {artifacts_dir}"
        )
    if len(matches) > 1:
        names = ", ".join(item.path.name for item in matches)
        raise SymbolBindingAmbiguousError(
            f"multiple symbol binding files match artifact_content_digest "
            f"{expected_digest}: {names}"
        )
    return matches[0]


# ── Artifact Identity Resolution for Run & Offline Case Lint ──────────────────


def resolve_artifact_identity_for_app(
    profile_or_app: AppProfile | str,
    *,
    build_id: str | None = None,
    build_identity_doc: Mapping[str, Any] | None = None,
    run_dir: Path | None = None,
    package_snapshot: Mapping[str, Any] | None = None,
    package_root: Path | None = None,
    code_root: Path | None = None,
    allow_default_build: bool = True,
) -> ResolvedArtifactIdentity:
    """Resolve the exact pure-content artifact identity for symbol routing.

    Supports three canonical callers (Decision D5):
    1. **Run mode** (``build_identity_doc`` or ``run_dir``): requires ``identity_state == "confirmed"``;
       supports both ``rn_candidate`` (from ``build-identity.v2.json``, strictly stripping
       ``target_fingerprint``) and ``ipa_build`` (confirmed Registry SHA / Package Store).
    2. **Explicit ``--build-id``** (Run or offline ``case lint``): resolves exact Build Record
       from Package Store.
    3. **Default / promoted active build** (offline ``case lint`` when ``allow_default_build=True``):
       resolves ``default_slot`` (or latest active slot candidate) from Package Store without
       reading Rig state or mutating ``profile.yaml``.
    """
    if isinstance(profile_or_app, AppProfile):
        app = profile_or_app.app
        bundle_id = profile_or_app.bundle_id
        resolved_pkg_root = package_root or profile_or_app.host_state_root
        resolved_code_root = code_root or profile_or_app.root
    else:
        app = str(profile_or_app or "").strip()
        if not app:
            raise SymbolBindingError("app name must be non-empty")
        bundle_id = None
        resolved_pkg_root = package_root or STATE_ROOT
        resolved_code_root = code_root

    # 1. Run mode: confirmed build-identity.json
    if build_identity_doc is not None or run_dir is not None:
        doc = build_identity_doc
        if doc is None and run_dir is not None:
            doc = load_build_identity_document(Path(run_dir))
            if doc is None:
                raise SymbolBindingMissingError(
                    f"confirmed build-identity.json not found or invalid in {run_dir}"
                )
        assert doc is not None
        doc_app = str(doc.get("app") or "")
        if doc_app != app:
            raise SymbolBindingMismatchError(
                f"build-identity app {doc_app!r} does not match expected app {app!r}"
            )
        identity_state = str(doc.get("identity_state") or "")
        if identity_state != "confirmed":
            raise SymbolBindingMismatchError(
                f"build-identity for app {app!r} is not confirmed "
                f"(identity_state={identity_state!r})"
            )

        device_obs = doc.get("device_observation")
        obs = device_obs if isinstance(device_obs, Mapping) else {}
        obs_bundle = str(obs.get("bundle_id") or bundle_id or "") or None
        obs_short = str(obs.get("short_version") or "") or None
        obs_build = str(obs.get("build_version") or "") or None

        candidate = doc.get("candidate_identity")
        if doc.get("schema_version") == "v2" and isinstance(candidate, Mapping):
            if build_id is not None and candidate.get("base_build_id") not in (None, "", build_id):
                raise SymbolBindingMismatchError(
                    f"rn_candidate base_build_id {candidate.get('base_build_id')!r} "
                    f"does not match requested build_id {build_id!r}"
                )
            rn_identity = artifact_identity_from_rn_candidate(
                candidate,
                short_version=obs_short or "",
                build_version=obs_build or "",
            )
            digest = canonical_artifact_content_digest(rn_identity)
            return ResolvedArtifactIdentity(
                app=app,
                kind="rn_candidate",
                artifact_content_digest=digest,
                source="build_identity_confirmed",
                build_id=None,
                bundle_id=str(candidate.get("bundle_id") or obs_bundle or "") or None,
                short_version=obs_short,
                build_version=obs_build,
                base_ipa_sha256=rn_identity["base_ipa_sha256"],
                main_jsbundle_sha256=rn_identity["main_jsbundle_sha256"],
                assets_tree_sha256=rn_identity["assets_tree_sha256"],
                identity_state="confirmed",
            )

        # Confirmed ipa_build: reconcile exact artifact_sha256 from match / package_snapshot / build_id
        match_obj = doc.get("match")
        match_sha = (
            _normalize_sha256(match_obj["matched_registry_sha256"], "matched_registry_sha256")
            if isinstance(match_obj, Mapping)
            and match_obj.get("matched_registry_sha256") not in (None, "")
            else None
        )
        match_build_id = (
            str(match_obj.get("matched_build_id") or "").strip() or None
            if isinstance(match_obj, Mapping)
            else None
        )
        pkg_ref = (
            package_snapshot.get("registry_build_ref")
            if isinstance(package_snapshot, Mapping)
            and isinstance(package_snapshot.get("registry_build_ref"), Mapping)
            else None
        )
        pkg_sha = (
            _normalize_sha256(pkg_ref["artifact_sha256"], "registry_build_ref.artifact_sha256")
            if pkg_ref is not None and pkg_ref.get("artifact_sha256") not in (None, "")
            else None
        )
        pkg_build_id = (
            str(pkg_ref.get("build_id") or "").strip() or None
            if pkg_ref is not None
            else None
        )
        if pkg_ref is not None and pkg_ref.get("app") not in (None, "", app):
            raise SymbolBindingMismatchError(
                f"package_snapshot registry_build_ref app {pkg_ref.get('app')!r} != {app!r}"
            )
        if match_build_id is not None:
            if build_id is not None and match_build_id != build_id:
                raise SymbolBindingMismatchError(
                    f"requested build_id {build_id!r} disagrees with "
                    f"build-identity matched_build_id {match_build_id!r}"
                )
            if pkg_build_id is not None and match_build_id != pkg_build_id:
                raise SymbolBindingMismatchError(
                    f"package_snapshot build_id {pkg_build_id!r} disagrees with "
                    f"build-identity matched_build_id {match_build_id!r}"
                )

        store_ref: PackageBuildInfo | None = None
        requested_build_id = build_id or pkg_build_id or match_build_id
        if build_id is not None:
            if pkg_build_id is not None and pkg_build_id != build_id:
                raise SymbolBindingMismatchError(
                    f"requested build_id {build_id!r} disagrees with "
                    f"package_snapshot build_id {pkg_build_id!r}"
                )
            store_ref = _resolve_store_build(
                app,
                build_id=build_id,
                package_root=resolved_pkg_root,
                code_root=resolved_code_root,
            )
        store_sha = (
            canonical_ipa_content_digest(store_ref.artifact_sha256)
            if store_ref is not None
            else None
        )

        shas = {s for s in (match_sha, pkg_sha, store_sha) if s is not None}
        if not shas:
            raise SymbolBindingMissingError(
                f"confirmed build-identity for app {app!r} lacks exact artifact_sha256"
            )
        if len(shas) > 1:
            raise SymbolBindingMismatchError(
                f"conflicting artifact_sha256 values for app {app!r}: {sorted(shas)}"
            )
        exact_sha = next(iter(shas))
        return ResolvedArtifactIdentity(
            app=app,
            kind="ipa_build",
            artifact_content_digest=exact_sha,
            source="build_identity_confirmed",
            build_id=requested_build_id,
            bundle_id=obs_bundle,
            short_version=obs_short or (store_ref.short_version if store_ref else None),
            build_version=obs_build or (store_ref.build_version if store_ref else None),
            artifact_sha256=exact_sha,
            identity_state="confirmed",
        )

    # 2. Explicit --build-id via Package Store
    if build_id is not None:
        ref = _resolve_store_build(
            app,
            build_id=build_id,
            package_root=resolved_pkg_root,
            code_root=resolved_code_root,
        )
        exact_sha = canonical_ipa_content_digest(ref.artifact_sha256)
        return ResolvedArtifactIdentity(
            app=app,
            kind="ipa_build",
            artifact_content_digest=exact_sha,
            source="explicit_build_id",
            build_id=ref.build_id,
            bundle_id=bundle_id,
            short_version=ref.short_version,
            build_version=ref.build_version,
            artifact_sha256=exact_sha,
        )

    # 3. Frozen package_snapshot with exact registry_build_ref
    if isinstance(package_snapshot, Mapping) and isinstance(
        package_snapshot.get("registry_build_ref"), Mapping
    ):
        pkg_ref = package_snapshot["registry_build_ref"]
        if pkg_ref.get("app") not in (None, "", app):
            raise SymbolBindingMismatchError(
                f"package_snapshot registry_build_ref app {pkg_ref.get('app')!r} != {app!r}"
            )
        exact_sha = canonical_ipa_content_digest(pkg_ref.get("artifact_sha256"))  # type: ignore[arg-type]
        return ResolvedArtifactIdentity(
            app=app,
            kind="ipa_build",
            artifact_content_digest=exact_sha,
            source="package_snapshot",
            build_id=str(pkg_ref.get("build_id") or "").strip() or None,
            bundle_id=bundle_id,
            short_version=str(pkg_ref.get("short_version") or "") or None,
            build_version=str(pkg_ref.get("build_version") or "") or None,
            artifact_sha256=exact_sha,
        )

    # 4. Offline promoted / active default build via Package Store (for case lint)
    if allow_default_build:
        ref = _resolve_store_build(
            app,
            build_id=None,
            package_root=resolved_pkg_root,
            code_root=resolved_code_root,
        )
        exact_sha = canonical_ipa_content_digest(ref.artifact_sha256)
        return ResolvedArtifactIdentity(
            app=app,
            kind="ipa_build",
            artifact_content_digest=exact_sha,
            source="package_store_default",
            build_id=ref.build_id,
            bundle_id=bundle_id,
            short_version=ref.short_version,
            build_version=ref.build_version,
            artifact_sha256=exact_sha,
        )

    raise SymbolBindingMissingError(
        f"cannot resolve artifact identity for app {app!r}: "
        "provide build_id, confirmed build-identity, or allow_default_build=True"
    )


def _resolve_store_build(
    app: str,
    build_id: str | None,
    *,
    package_root: Path,
    code_root: Path | None,
) -> PackageBuildInfo:
    try:
        return resolve_package_manifest_build(
            app,
            build_id=build_id,
            root=package_root,
            code_root=code_root,
        )
    except PackageManifestError as exc:
        raise SymbolBindingMissingError(
            f"unable to resolve package store build for app {app!r}: {exc}"
        ) from exc


def load_symbol_binding_for_app(
    profile: AppProfile,
    *,
    build_id: str | None = None,
    build_identity_doc: Mapping[str, Any] | None = None,
    run_dir: Path | None = None,
    package_snapshot: Mapping[str, Any] | None = None,
    package_root: Path | None = None,
    allow_default_build: bool = True,
) -> LoadedSymbolBinding:
    """Resolve the app's exact artifact identity and load its unique symbol binding."""
    resolved = resolve_artifact_identity_for_app(
        profile,
        build_id=build_id,
        build_identity_doc=build_identity_doc,
        run_dir=run_dir,
        package_snapshot=package_snapshot,
        package_root=package_root,
        allow_default_build=allow_default_build,
    )
    return load_symbol_binding(
        profile,
        artifact_content_digest=resolved.artifact_content_digest,
        expected_app=profile.app,
        expected_build_id=resolved.build_id if resolved.kind == "ipa_build" else None,
    )


# ── External Claims Store (apps/<app>/symbols/claims.json) ────────────────────


def _resolve_claims_app(
    app_dir_or_profile: Path | AppProfile,
    app: str | None,
) -> tuple[Path, str]:
    app_dir, profile_app = _resolve_app_dir(app_dir_or_profile)
    resolved_app = str(app or profile_app or app_dir.name).strip()
    if not resolved_app:
        raise SymbolClaimError("app identifier must be non-empty")
    if profile_app is not None and app is not None and app != profile_app:
        raise SymbolClaimError(
            f"claims app {app!r} does not match profile app {profile_app!r}"
        )
    return symbol_claims_path(app_dir), resolved_app


def load_symbol_claims(
    app_dir_or_profile: Path | AppProfile,
    *,
    app: str | None = None,
) -> dict[str, Any]:
    """Load and schema-validate ``apps/<app>/symbols/claims.json`` (or empty v1 envelope)."""
    path, resolved_app = _resolve_claims_app(app_dir_or_profile, app)
    if path.is_symlink():
        raise SymbolClaimError(f"symbol claims path must not be a symlink: {path}")
    if not path.exists():
        return {"schema_version": "v1", "app": resolved_app, "claims": []}
    try:
        raw = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise SymbolClaimError(f"failed to read symbol claims {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SymbolClaimError(f"symbol claims {path} must be a JSON object")
    try:
        validate_or_raise(KNOWLEDGE_CLAIMS_SCHEMA_FILE, raw)
    except ContractError as exc:
        raise SymbolClaimError(f"symbol claims {path} failed schema validation: {exc}") from exc
    if raw.get("app") != resolved_app:
        raise SymbolClaimError(
            f"symbol claims {path} app {raw.get('app')!r} does not match {resolved_app!r}"
        )
    return raw


def save_symbol_claims(
    app_dir_or_profile: Path | AppProfile,
    claims_or_doc: Mapping[str, Any] | Iterable[dict[str, Any]],
    *,
    app: str | None = None,
) -> Path:
    """Atomically persist ``apps/<app>/symbols/claims.json`` under ``path_write_lock``."""
    path, resolved_app = _resolve_claims_app(app_dir_or_profile, app)
    if path.is_symlink():
        raise SymbolClaimError(f"symbol claims path must not be a symlink: {path}")

    if isinstance(claims_or_doc, Mapping):
        payload = copy.deepcopy(dict(claims_or_doc))
        if payload.get("app") != resolved_app:
            raise SymbolClaimError(
                f"claims document app {payload.get('app')!r} does not match {resolved_app!r}"
            )
    else:
        payload = {
            "schema_version": "v1",
            "app": resolved_app,
            "claims": [copy.deepcopy(dict(row)) for row in claims_or_doc],
        }

    with path_write_lock(path):
        try:
            validate_or_raise(KNOWLEDGE_CLAIMS_SCHEMA_FILE, payload)
        except ContractError as exc:
            raise SymbolClaimError(
                f"symbol claims validation failed before save: {exc}"
            ) from exc
        write_json_atomic(path, payload)
    return path


def append_symbol_claim(
    app_dir_or_profile: Path | AppProfile,
    claim: Mapping[str, Any],
    *,
    app: str | None = None,
) -> dict[str, Any]:
    """Append one schema-valid claim to ``apps/<app>/symbols/claims.json`` atomically."""
    if not isinstance(claim, Mapping):
        raise SymbolClaimError("claim must be a mapping")
    entry = copy.deepcopy(dict(claim))
    provenance = entry.get("provenance")
    scope = entry.get("scope")
    if (
        entry.get("kind") == CLAIM_KIND_UNIQUE_VISIBILITY
        and isinstance(provenance, Mapping)
        and provenance.get("source") == "vm_observation"
    ):
        state = scope.get("identity_state") if isinstance(scope, Mapping) else None
        if state != "confirmed":
            raise SymbolClaimError(
                "vm_observation symbol.unique_visibility claim requires "
                f"scope.identity_state == 'confirmed', got {state!r}"
            )

    path, resolved_app = _resolve_claims_app(app_dir_or_profile, app)
    with path_write_lock(path):
        current = load_symbol_claims(app_dir_or_profile, app=resolved_app)
        claim_id = str(entry.get("id") or "")
        if any(str(row.get("id")) == claim_id for row in current["claims"]):
            raise SymbolClaimError(f"duplicate symbol claim id: {claim_id!r}")
        updated = {
            "schema_version": "v1",
            "app": resolved_app,
            "claims": [*current["claims"], entry],
        }
        try:
            validate_or_raise(KNOWLEDGE_CLAIMS_SCHEMA_FILE, updated)
        except ContractError as exc:
            raise SymbolClaimError(f"symbol claim failed schema validation: {exc}") from exc
        write_json_atomic(path, updated)
    return entry


def record_unique_visibility_claim(
    app_dir_or_profile: Path | AppProfile,
    *,
    symbol_id: str,
    artifact_content_digest: str,
    binding_sha256: str,
    screen_id: str,
    identity_state: str | None,
    ax_backend: str,
    evidence_refs: Sequence[str] = (),
    artifact_sha256: str | None = None,
    statement: str | None = None,
    claim_id: str | None = None,
    app: str | None = None,
    provenance_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a runtime ``vm_observation`` ``symbol.unique_visibility`` claim.

    Strictly rejects any ``identity_state != "confirmed"`` (including ``ambiguous``,
    ``mismatch``, ``unavailable``, and ``version_only``). Never modifies any symbol YAML
    file under ``symbols/artifacts/`` so ``binding_sha256`` remains invariant.
    """
    if identity_state != "confirmed":
        raise SymbolClaimError(
            "cannot write vm_observation symbol.unique_visibility claim unless "
            f"identity_state == 'confirmed' (got {identity_state!r})"
        )
    if not isinstance(symbol_id, str) or not symbol_id.strip():
        raise SymbolClaimError("symbol_id must be a non-empty string")
    if not isinstance(screen_id, str) or not screen_id.strip():
        raise SymbolClaimError("screen_id must be a non-empty string")
    if not isinstance(ax_backend, str) or ax_backend not in VALID_AX_BACKENDS:
        raise SymbolClaimError(
            f"invalid ax_backend {ax_backend!r}: expected one of {sorted(VALID_AX_BACKENDS)}"
        )

    digest = _normalize_sha256(artifact_content_digest, "artifact_content_digest")
    binding = _normalize_sha256(binding_sha256, "binding_sha256")
    scope: dict[str, Any] = {
        "symbol_id": symbol_id.strip(),
        "artifact_content_digest": digest,
        "binding_sha256": binding,
        "screen_id": screen_id.strip(),
        "identity_state": "confirmed",
        "ax_backend": ax_backend,
    }
    if artifact_sha256 is not None:
        normalized_sha = _normalize_sha256(artifact_sha256, "artifact_sha256")
        if normalized_sha != digest:
            raise SymbolClaimError(
                f"artifact_sha256 {normalized_sha} does not match "
                f"artifact_content_digest {digest}"
            )
        scope["artifact_sha256"] = normalized_sha

    provenance: dict[str, Any] = dict(provenance_extra or {})
    provenance["source"] = "vm_observation"

    refs = [str(ref).strip() for ref in evidence_refs if str(ref).strip()]
    claim: dict[str, Any] = {
        "id": (str(claim_id).strip() if claim_id else f"clm-sym-{uuid.uuid4().hex[:12]}"),
        "status": "active",
        "kind": CLAIM_KIND_UNIQUE_VISIBILITY,
        "statement": (
            str(statement).strip()
            if statement and str(statement).strip()
            else f"{symbol_id.strip()} uniquely visible on {screen_id.strip()} via {ax_backend}"
        ),
        "evidence_level": "observed",
        "evidence_state": "current",
        "provenance": provenance,
        "scope": scope,
        "evidence_refs": refs,
    }
    return append_symbol_claim(app_dir_or_profile, claim, app=app)


def _claim_scope_digest_matches(scope: Mapping[str, Any], expected_digest: str) -> bool:
    raw_content_digest = scope.get("artifact_content_digest")
    raw_artifact_sha = scope.get("artifact_sha256")
    present: list[str] = []
    for raw in (raw_content_digest, raw_artifact_sha):
        if raw is None:
            continue
        if not isinstance(raw, str):
            return False
        text = raw.strip().lower()
        if not _SHA256_RE.fullmatch(text):
            return False
        present.append(text)
    return bool(present) and all(item == expected_digest for item in present)


def has_reusable_unique_visibility(
    claims_doc: Mapping[str, Any] | None,
    *,
    symbol_id: str,
    artifact_content_digest: str,
    binding_sha256: str,
    screen_id: str,
    ax_backend: str,
) -> bool:
    """Return True iff an active/current confirmed ``vm_observation`` claim matches all 5 keys.

    Exact match required on ALL FIVE of:
      ``(symbol_id, artifact_content_digest, binding_sha256, screen_id, ax_backend)``
    plus ``identity_state == "confirmed"``, ``status == "active"``,
    ``evidence_state == "current"``, ``provenance.source == "vm_observation"``, and
    observed (non-``static_hint``) ``evidence_level``.
    """
    if not isinstance(claims_doc, Mapping):
        return False
    if not symbol_id or not screen_id or ax_backend not in VALID_AX_BACKENDS:
        return False
    try:
        expected_digest = _normalize_sha256(artifact_content_digest, "artifact_content_digest")
        expected_binding = _normalize_sha256(binding_sha256, "binding_sha256")
    except SymbolBindingError:
        return False

    claims = claims_doc.get("claims")
    if not isinstance(claims, list):
        return False

    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        if claim.get("kind") != CLAIM_KIND_UNIQUE_VISIBILITY:
            continue
        if claim.get("status") != "active":
            continue
        if claim.get("evidence_state") != "current":
            continue
        if claim.get("evidence_level") not in _OBSERVED_EVIDENCE_LEVELS:
            continue
        provenance = claim.get("provenance")
        if not isinstance(provenance, Mapping) or provenance.get("source") != "vm_observation":
            continue
        scope = claim.get("scope")
        if not isinstance(scope, Mapping):
            continue
        if scope.get("identity_state") != "confirmed":
            continue
        if scope.get("symbol_id") != symbol_id:
            continue
        if not _claim_scope_digest_matches(scope, expected_digest):
            continue
        raw_binding = scope.get("binding_sha256")
        if not isinstance(raw_binding, str) or raw_binding.strip().lower() != expected_binding:
            continue
        if scope.get("screen_id") != screen_id:
            continue
        if scope.get("ax_backend") != ax_backend:
            continue
        return True
    return False


__all__ = [
    "APP_SYMBOLS_SCHEMA",
    "APP_SYMBOLS_SCHEMA_FILE",
    "ArtifactKind",
    "ArtifactResolutionSource",
    "CLAIM_KIND_EFFECT_SET",
    "CLAIM_KIND_LOCATOR_BINDING",
    "CLAIM_KIND_UNIQUE_VISIBILITY",
    "EffectCompleteness",
    "KNOWLEDGE_CLAIMS_SCHEMA_FILE",
    "LoadedSymbolBinding",
    "LocatorKind",
    "ResolvedArtifactIdentity",
    "SymbolBindingAmbiguousError",
    "SymbolBindingError",
    "SymbolBindingMismatchError",
    "SymbolBindingMissingError",
    "SymbolClaimError",
    "SymbolControlSpec",
    "SymbolEffectSpec",
    "SymbolLocatorSpec",
    "SymbolScopeSpec",
    "append_symbol_claim",
    "artifact_filename_for_digest",
    "artifact_identity_from_build_ref",
    "artifact_identity_from_rn_candidate",
    "canonical_artifact_content_digest",
    "canonical_ipa_content_digest",
    "canonical_rn_candidate_content_digest",
    "has_reusable_unique_visibility",
    "load_symbol_binding",
    "load_symbol_binding_for_app",
    "load_symbol_binding_file",
    "load_symbol_claims",
    "record_unique_visibility_claim",
    "resolve_artifact_identity_for_app",
    "save_symbol_claims",
    "symbol_claims_path",
    "symbols_artifacts_dir",
    "symbols_dir",
]
