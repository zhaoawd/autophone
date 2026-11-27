"""Lightweight symbol toolchain for ``app-symbols.v1`` (Milestone M6).

Implements Decision D8's two narrow entrypoints plus §4.4 dual-denominator metrics:
- **L2 Thin Manifest Importer** (``import_l2_manifest``): converts a build-side
  JSON/YAML manifest plus exact artifact identity (from Package Store ``--build-id``
  or explicit ``ipa_build``/``rn_candidate`` identity with ``target_fingerprint``
  stripped) into an immutable ``apps/<app>/symbols/artifacts/<digest[:12]>.yaml``
  and appends ``l2_build_manifest`` (``static_hint``) claims into external
  ``apps/<app>/symbols/claims.json``.
- **L1 Minimal RN Blind Extractor** (``extract_l1_rn_symbols``): inspects an
  unencrypted React Native IPA (or standalone ``main.jsbundle`` with explicit
  ``rn_candidate`` identity), extracts stable ``testID`` / ``accessibilityIdentifier``
  candidates from plain JS or Hermes bytecode bundles, writes a compliant draft
  binding with ``effects: {verbs: [], completeness: "unknown"}``, and records
  ``l1_ipa_extractor`` (``static_hint``) claims externally.
- **Dual-Denominator Coverage Calculator** (``compute_dual_denominator_metrics``):
  pure calculator for candidate usability rate and business target coverage.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import plistlib
import re
from typing import Any, Iterable, Literal, Mapping
import zipfile

import yaml

from autophone.assets.app_symbols import (
    APP_SYMBOLS_SCHEMA,
    APP_SYMBOLS_SCHEMA_FILE,
    CLAIM_KIND_EFFECT_SET,
    CLAIM_KIND_LOCATOR_BINDING,
    LoadedSymbolBinding,
    SymbolBindingError,
    SymbolBindingMismatchError,
    artifact_filename_for_digest,
    artifact_identity_from_rn_candidate,
    canonical_artifact_content_digest,
    canonical_ipa_content_digest,
    load_symbol_binding_file,
    load_symbol_claims,
    resolve_artifact_identity_for_app,
    save_symbol_claims,
    symbol_claims_path,
    symbols_artifacts_dir,
)
from autophone.assets._package_manifest import lookup_package_manifest_bundle_id
from autophone.assets.apps import AppProfile
from autophone.assets.store import path_write_lock
from autophone.contracts import ContractError, validate_or_raise
from autophone.paths import APP_ASSET_ROOT, STATE_ROOT
from autophone.util import write_text_atomic

PROVENANCE_L2_MANIFEST = "l2_build_manifest"
PROVENANCE_L1_EXTRACTOR = "l1_ipa_extractor"
DEFAULT_L1_SCREEN_ID = "UNASSIGNED"
DEFAULT_L1_SYMBOL_PREFIX = "ctrl.rn"

# Standard Hermes bytecode header magic (little-endian 0x1f1903c103bc1fc6).
HERMES_BYTECODE_MAGIC = b"\xc6\x1f\xbc\x03\xc1\x03\x19\x1f"

BundleFormat = Literal["plain_js", "hermes_bytecode"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FORBIDDEN_SYMBOL_IDS = frozenset({"relation_claims", "target_fingerprint"})
_VALID_COMPLETENESS = frozenset({"exhaustive", "lower_bound", "unknown"})

# Plain JS / JSX literal testID or accessibilityIdentifier attributes.
_JS_ID_ATTR_RE = re.compile(
    r"""(?<![A-Za-z0-9_$.])(?:['"])?(testID|accessibilityIdentifier)(?:['"])?\s*[:=]\s*(['"])((?:\\.|(?!\2)[^\\\r\n])*)\2"""
)
_JS_DYNAMIC_TAIL_RE = re.compile(r"^\s*(?:\+|\.concat\s*\()")
_JS_ROLE_RE = re.compile(
    r"""(?:accessibilityRole|role)\s*[:=]\s*['"]([A-Za-z0-9_-]+)['"]"""
)
_JS_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

# Strict candidate identifier validator (filters dynamic templates, URLs, whitespace, noise).
_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,126}[A-Za-z0-9]$")
_TEMPLATE_NOISE_RE = re.compile(r"(?:\$\{|\{\{|\}\}|%[sdif]|:[A-Za-z_][A-Za-z0-9_]*$|\{[A-Za-z0-9_]+\})")

# Hermes adjacent marker scan and high-precision lowercase dotted control IDs.
_HERMES_ADJACENT_RE = re.compile(
    rb"(?:testID|accessibilityIdentifier)\x00+([\x21-\x7e]{2,128})(?:\x00|$)"
)
_HERMES_PRINTABLE_TOKEN_RE = re.compile(rb"[\x21-\x7e]{3,128}")
_HERMES_DOTTED_CONTROL_RE = re.compile(
    r"^[a-z][a-z0-9_-]{1,31}(?:\.[a-z][a-z0-9_-]{1,31}){1,5}$"
)
_HERMES_NOISE_EXTENSIONS = frozenset({
    "js", "jsx", "ts", "tsx", "cjs", "mjs", "json", "map", "hbc", "bundle",
    "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "heic",
    "ttf", "otf", "woff", "woff2", "mp3", "mp4", "wav", "m4a", "mov",
    "plist", "strings", "nib", "storyboard", "storyboardc", "xcassets",
    "html", "htm", "css", "scss", "xml", "yaml", "yml", "md", "txt",
    "ios", "android", "native", "web", "min", "dev", "prod", "local",
    "com", "org", "net", "io", "app", "gov", "edu", "cn",
})
_HERMES_NOISE_PREFIXES = frozenset({
    "com", "org", "net", "io", "www", "http", "https", "mailto", "tel",
    "react", "reactjs", "rn", "expo", "sentry", "hermes", "metro", "babel",
    "node", "npm", "yarn", "pnpm", "console", "window", "document", "global",
    "globalthis", "object", "array", "string", "number", "boolean", "symbol",
    "promise", "error", "math", "json", "date", "regexp", "map", "set",
    "this", "self", "props", "state", "style", "styles", "theme", "router",
    "navigation", "module", "exports", "process", "buffer", "intl",
    "accessibility", "uikit", "foundation", "coregraphics", "cfbundle",
})


class SymbolToolError(SymbolBindingError):
    """Raised when L2 manifest import or L1 blind extraction fails closed."""

    code: str = "symbol_tool_error"


@dataclass(frozen=True)
class ExtractedControlCandidate:
    """One deterministic candidate control extracted from a React Native bundle."""

    identifier: str
    attribute: str
    inferred_kind: str = "control"


@dataclass(frozen=True)
class BundleExtractionResult:
    """Result of scanning a React Native ``main.jsbundle`` payload."""

    bundle_format: BundleFormat
    main_jsbundle_sha256: str
    candidates: tuple[ExtractedControlCandidate, ...]

    @property
    def identifiers(self) -> list[str]:
        return [item.identifier for item in self.candidates]


# ── Shared Path & Persistence Helpers ─────────────────────────────────────────


def _resolve_target_app_dir(
    app_or_profile: str | AppProfile,
    *,
    output_root: Path | None = None,
    app_dir: Path | None = None,
) -> tuple[str, Path, AppProfile | None]:
    if isinstance(app_or_profile, AppProfile):
        app_name = app_or_profile.app
        profile: AppProfile | None = app_or_profile
    else:
        app_name = str(app_or_profile or "").strip()
        if not app_name:
            raise SymbolToolError("app identifier must be non-empty")
        profile = None

    if app_dir is not None:
        resolved_dir = Path(app_dir)
    elif output_root is not None:
        resolved_dir = Path(output_root) / "apps" / app_name
    elif profile is not None:
        resolved_dir = profile.profile_dir
    else:
        resolved_dir = APP_ASSET_ROOT / "apps" / app_name
    return app_name, resolved_dir, profile


def _normalize_sha256_value(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SymbolToolError(f"{field_name} must be a 64-hex sha256 string, got {value!r}")
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise SymbolToolError(f"{field_name} must be a 64-hex sha256 string, got {value!r}")
    return normalized


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SymbolToolError(f"{field_name} must be a non-empty string")
    return value.strip()


def _serialize_symbol_yaml(doc: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(doc), allow_unicode=True, sort_keys=False)


def _write_and_verify_symbol_binding(
    app_dir: Path,
    *,
    app_name: str,
    doc: dict[str, Any],
    overwrite: bool = False,
) -> LoadedSymbolBinding:
    """Validate, atomically write, and single-read verify an ``app-symbols.v1`` file."""
    try:
        validate_or_raise(APP_SYMBOLS_SCHEMA_FILE, doc)
    except ContractError as exc:
        raise SymbolToolError(f"generated app-symbols.v1 document failed schema validation: {exc}") from exc

    digest = canonical_artifact_content_digest(doc["artifact_identity"])
    artifacts_dir = symbols_artifacts_dir(app_dir)
    short_path = artifacts_dir / artifact_filename_for_digest(digest, full=False)
    full_path = artifacts_dir / artifact_filename_for_digest(digest, full=True)

    serialized_text = _serialize_symbol_yaml(doc)
    serialized_bytes = serialized_text.encode("utf-8")

    target_path = short_path
    with path_write_lock(short_path):
        if short_path.is_symlink():
            raise SymbolToolError(f"symbol binding path must not be a symlink: {short_path}")
        if short_path.exists():
            existing_bytes = short_path.read_bytes()
            if existing_bytes == serialized_bytes:
                target_path = short_path
            else:
                existing_digest: str | None = None
                try:
                    existing_doc = yaml.safe_load(existing_bytes.decode("utf-8"))
                    if isinstance(existing_doc, Mapping) and isinstance(
                        existing_doc.get("artifact_identity"), Mapping
                    ):
                        existing_digest = canonical_artifact_content_digest(
                            existing_doc["artifact_identity"]
                        )
                except Exception:  # noqa: BLE001
                    existing_digest = None

                if existing_digest is not None and existing_digest != digest:
                    # 12-hex prefix collision with a distinct artifact: use full 64-hex filename.
                    target_path = full_path
                elif not overwrite:
                    raise SymbolToolError(
                        f"symbol binding already exists with different content: {short_path} "
                        "(pass overwrite=True to replace)"
                    )
                else:
                    write_text_atomic(short_path, serialized_text)
        else:
            write_text_atomic(short_path, serialized_text)

        if target_path == full_path:
            if full_path.is_symlink():
                raise SymbolToolError(f"symbol binding path must not be a symlink: {full_path}")
            if full_path.exists():
                if full_path.read_bytes() != serialized_bytes:
                    if not overwrite:
                        raise SymbolToolError(
                            f"symbol binding already exists with different content: {full_path} "
                            "(pass overwrite=True to replace)"
                        )
                    write_text_atomic(full_path, serialized_text)
            else:
                write_text_atomic(full_path, serialized_text)

    expected_build_id = (
        str(doc["artifact_identity"]["build_id"])
        if doc["artifact_identity"].get("kind") == "ipa_build"
        else None
    )
    return load_symbol_binding_file(
        target_path,
        expected_app=app_name,
        expected_artifact_content_digest=digest,
        expected_build_id=expected_build_id,
    )


def _deterministic_claim_id(
    prefix: str,
    *,
    source: str,
    kind: str,
    symbol_id: str,
    artifact_content_digest: str,
    binding_sha256: str,
) -> str:
    raw = "|".join((source, kind, symbol_id, artifact_content_digest, binding_sha256))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _base_claim_scope(
    binding: LoadedSymbolBinding,
    *,
    symbol_id: str,
    screen_id: str,
) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "symbol_id": symbol_id,
        "artifact_content_digest": binding.artifact_content_digest,
        "binding_sha256": binding.binding_sha256,
        "screen_id": screen_id,
    }
    if binding.artifact_kind == "ipa_build":
        scope["artifact_sha256"] = binding.artifact_content_digest
        if binding.build_id:
            scope["build_id"] = binding.build_id
    return scope


def _append_claims_batch(
    app_dir: Path,
    *,
    app_name: str,
    claims: list[dict[str, Any]],
) -> tuple[Path, int, int]:
    """Append schema-valid claims into ``apps/<app>/symbols/claims.json`` idempotently."""
    path = symbol_claims_path(app_dir)
    if path.is_symlink():
        raise SymbolToolError(f"symbol claims path must not be a symlink: {path}")

    with path_write_lock(path):
        current = load_symbol_claims(app_dir, app=app_name)
        existing_ids = {str(row.get("id")) for row in current["claims"]}
        to_add: list[dict[str, Any]] = []
        skipped = 0
        for claim in claims:
            cid = str(claim["id"])
            if cid in existing_ids:
                skipped += 1
                continue
            existing_ids.add(cid)
            to_add.append(claim)
        if to_add or not path.exists():
            updated = {
                "schema_version": "v1",
                "app": app_name,
                "claims": [*current["claims"], *to_add],
            }
            save_symbol_claims(app_dir, updated, app=app_name)
    return path, len(to_add), skipped


# ── L2 Thin Manifest Importer ─────────────────────────────────────────────────


def _load_manifest_input(
    manifest: str | Path | Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    if isinstance(manifest, Mapping):
        return dict(manifest), PROVENANCE_L2_MANIFEST
    path = Path(manifest)
    if not path.is_file():
        raise SymbolToolError(f"L2 manifest file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            parsed = json.loads(text)
        else:
            parsed = yaml.safe_load(text)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise SymbolToolError(f"failed to parse L2 manifest {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SymbolToolError(f"L2 manifest {path} must be a JSON/YAML object")
    return parsed, str(path)


def _strip_and_normalize_explicit_identity(
    raw_identity: Mapping[str, Any],
    *,
    profile: AppProfile | None = None,
) -> dict[str, Any]:
    """Normalize an explicit ``ipa_build`` or ``rn_candidate`` identity, strictly stripping ``target_fingerprint``."""
    if not isinstance(raw_identity, Mapping):
        raise SymbolToolError("artifact_identity must be a mapping")
    cleaned = {k: v for k, v in raw_identity.items() if k != "target_fingerprint"}
    raw_kind = cleaned.get("kind")
    rn_keys = ("base_ipa_sha256", "main_jsbundle_sha256", "assets_tree_sha256")
    has_rn_triplet = all(cleaned.get(k) not in (None, "") for k in rn_keys)
    has_ipa_sha = cleaned.get("artifact_sha256") not in (None, "")

    if raw_kind == "rn_candidate" or (raw_kind is None and has_rn_triplet and not has_ipa_sha):
        if "artifact_sha256" in cleaned:
            raise SymbolToolError("rn_candidate artifact_identity must not include artifact_sha256")
        rn_dict = artifact_identity_from_rn_candidate(
            cleaned,
            short_version=str(cleaned.get("short_version") or ""),
            build_version=str(cleaned.get("build_version") or ""),
        )
        if not rn_dict["bundle_id"] and profile is not None:
            rn_dict["bundle_id"] = profile.bundle_id
        for key in ("bundle_id", "short_version", "build_version"):
            _require_non_empty_str(rn_dict.get(key), f"artifact_identity.{key}")
        return rn_dict

    if raw_kind == "ipa_build" or (raw_kind is None and has_ipa_sha and not has_rn_triplet):
        if any(k in cleaned for k in rn_keys):
            raise SymbolToolError("ipa_build artifact_identity must not include rn_candidate hashes")
        bundle_id = cleaned.get("bundle_id") or (profile.bundle_id if profile is not None else None)
        return {
            "kind": "ipa_build",
            "bundle_id": _require_non_empty_str(bundle_id, "artifact_identity.bundle_id"),
            "short_version": _require_non_empty_str(
                cleaned.get("short_version"), "artifact_identity.short_version"
            ),
            "build_version": _require_non_empty_str(
                cleaned.get("build_version"), "artifact_identity.build_version"
            ),
            "build_id": _require_non_empty_str(cleaned.get("build_id"), "artifact_identity.build_id"),
            "artifact_sha256": canonical_ipa_content_digest(cleaned.get("artifact_sha256")),  # type: ignore[arg-type]
        }

    raise SymbolToolError(
        "artifact_identity must specify either ipa_build (artifact_sha256 + build_id) "
        "or rn_candidate (base_ipa_sha256, main_jsbundle_sha256, assets_tree_sha256)"
    )


def _lookup_package_store_bundle_id(
    app_name: str,
    build_id: str,
    *,
    package_root: Path,
    code_root: Path | None,
) -> str | None:
    return lookup_package_manifest_bundle_id(
        app_name,
        build_id,
        root=package_root,
        code_root=code_root,
    )


def _resolve_l2_artifact_identity(
    app_name: str,
    *,
    profile: AppProfile | None,
    manifest_doc: Mapping[str, Any],
    build_id: str | None,
    artifact_identity: Mapping[str, Any] | None,
    package_root: Path | None,
    code_root: Path | None,
) -> dict[str, Any]:
    raw_explicit = artifact_identity if artifact_identity is not None else manifest_doc.get("artifact_identity")
    explicit_clean = (
        _strip_and_normalize_explicit_identity(raw_explicit, profile=profile)
        if raw_explicit is not None
        else None
    )

    if build_id is not None:
        if explicit_clean is not None and explicit_clean["kind"] == "rn_candidate":
            raise SymbolBindingMismatchError(
                "--build-id cannot be combined with an rn_candidate artifact_identity"
            )
        resolved_pkg_root = (
            Path(package_root)
            if package_root is not None
            else (profile.host_state_root if profile is not None else STATE_ROOT)
        )
        resolved_code_root = (
            Path(code_root)
            if code_root is not None
            else (profile.root if profile is not None else None)
        )
        resolved = resolve_artifact_identity_for_app(
            profile if profile is not None else app_name,
            build_id=build_id,
            package_root=resolved_pkg_root,
            code_root=resolved_code_root,
            allow_default_build=False,
        )
        bundle_id = (
            resolved.bundle_id
            or _lookup_package_store_bundle_id(
                app_name,
                resolved.build_id or build_id,
                package_root=resolved_pkg_root,
                code_root=resolved_code_root,
            )
            or (explicit_clean.get("bundle_id") if explicit_clean else None)
            or manifest_doc.get("bundle_id")
        )
        short_version = (
            resolved.short_version
            or (explicit_clean.get("short_version") if explicit_clean else None)
        )
        build_version = (
            resolved.build_version
            or (explicit_clean.get("build_version") if explicit_clean else None)
        )
        store_identity = {
            "kind": "ipa_build",
            "bundle_id": _require_non_empty_str(bundle_id, "artifact_identity.bundle_id"),
            "short_version": _require_non_empty_str(short_version, "artifact_identity.short_version"),
            "build_version": _require_non_empty_str(build_version, "artifact_identity.build_version"),
            "build_id": _require_non_empty_str(resolved.build_id, "artifact_identity.build_id"),
            "artifact_sha256": canonical_ipa_content_digest(resolved.artifact_sha256 or ""),
        }
        if explicit_clean is not None:
            for field in ("build_id", "artifact_sha256", "bundle_id"):
                if explicit_clean.get(field) != store_identity[field]:
                    raise SymbolBindingMismatchError(
                        f"L2 manifest artifact_identity.{field} {explicit_clean.get(field)!r} "
                        f"contradicts Package Store value {store_identity[field]!r}"
                    )
        return store_identity

    if explicit_clean is not None:
        return explicit_clean

    raise SymbolToolError(
        "L2 manifest import requires either --build-id (Package Store) "
        "or an explicit artifact_identity (ipa_build or rn_candidate)"
    )


def _normalize_region(raw_region: Any, symbol_id: str) -> list[float] | None:
    if raw_region is None:
        return None
    if not isinstance(raw_region, (list, tuple)) or len(raw_region) != 4:
        raise SymbolToolError(f"control {symbol_id!r} scope.region must be a 4-element [nx0, ny0, nx1, ny1] array")
    values: list[float] = []
    for item in raw_region:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise SymbolToolError(f"control {symbol_id!r} scope.region coordinates must be numbers")
        fval = float(item)
        if fval < 0.0 or fval > 1.0:
            raise SymbolToolError(
                f"control {symbol_id!r} scope.region coordinate {fval} out of [0.0, 1.0]"
            )
        values.append(fval)
    return values


def _normalize_l2_control(
    symbol_id: str,
    raw_control: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    if not isinstance(symbol_id, str) or not _SYMBOL_ID_RE.fullmatch(symbol_id):
        raise SymbolToolError(f"invalid symbol_id {symbol_id!r}")
    if symbol_id in _FORBIDDEN_SYMBOL_IDS:
        raise SymbolToolError(f"forbidden symbol_id {symbol_id!r}")
    if not isinstance(raw_control, Mapping):
        raise SymbolToolError(f"control {symbol_id!r} must be a mapping")

    allowed_keys = {"symbol_id", "kind", "scope", "locator", "effects"}
    unknown = set(raw_control) - allowed_keys
    if unknown:
        raise SymbolToolError(f"control {symbol_id!r} has unexpected keys: {sorted(unknown)}")

    kind = _require_non_empty_str(raw_control.get("kind"), f"controls.{symbol_id}.kind")

    raw_scope = raw_control.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise SymbolToolError(f"control {symbol_id!r} requires a scope mapping")
    unknown_scope = set(raw_scope) - {"screen_id", "region"}
    if unknown_scope:
        raise SymbolToolError(
            f"control {symbol_id!r} scope has unexpected keys: {sorted(unknown_scope)}"
        )
    screen_id = _require_non_empty_str(raw_scope.get("screen_id"), f"controls.{symbol_id}.scope.screen_id")
    region = _normalize_region(raw_scope.get("region"), symbol_id)
    scope: dict[str, Any] = {"screen_id": screen_id}
    if region is not None:
        scope["region"] = region

    raw_locator = raw_control.get("locator")
    if not isinstance(raw_locator, Mapping):
        raise SymbolToolError(f"control {symbol_id!r} requires a locator mapping")
    unknown_loc = set(raw_locator) - {"kind", "identifier", "role", "enabled_only", "require_unique"}
    if unknown_loc:
        raise SymbolToolError(
            f"control {symbol_id!r} locator has unexpected keys: {sorted(unknown_loc)}"
        )
    loc_kind = str(raw_locator.get("kind") or "ax_identifier")
    if loc_kind != "ax_identifier":
        raise SymbolToolError(
            f"control {symbol_id!r} locator.kind must be 'ax_identifier', got {loc_kind!r}"
        )
    identifier = _require_non_empty_str(
        raw_locator.get("identifier"), f"controls.{symbol_id}.locator.identifier"
    )
    raw_role = raw_locator.get("role")
    if raw_role is not None:
        role: str | None = _require_non_empty_str(raw_role, f"controls.{symbol_id}.locator.role")
    else:
        role = None
    enabled_only = raw_locator.get("enabled_only", True)
    if not isinstance(enabled_only, bool):
        raise SymbolToolError(f"control {symbol_id!r} locator.enabled_only must be boolean")
    require_unique = raw_locator.get("require_unique", True)
    if not isinstance(require_unique, bool):
        raise SymbolToolError(f"control {symbol_id!r} locator.require_unique must be boolean")
    locator: dict[str, Any] = {
        "kind": "ax_identifier",
        "identifier": identifier,
        "role": role,
        "enabled_only": enabled_only,
        "require_unique": require_unique,
    }

    effects_declared = "effects" in raw_control and raw_control["effects"] is not None
    if effects_declared:
        raw_effects = raw_control["effects"]
        if not isinstance(raw_effects, Mapping):
            raise SymbolToolError(f"control {symbol_id!r} effects must be a mapping")
        unknown_eff = set(raw_effects) - {"verbs", "completeness"}
        if unknown_eff:
            raise SymbolToolError(
                f"control {symbol_id!r} effects has unexpected keys: {sorted(unknown_eff)}"
            )
        raw_verbs = raw_effects.get("verbs")
        if not isinstance(raw_verbs, list):
            raise SymbolToolError(f"control {symbol_id!r} effects.verbs must be a list of strings")
        verbs: list[str] = []
        seen_verbs: set[str] = set()
        for verb in raw_verbs:
            cleaned_verb = _require_non_empty_str(verb, f"controls.{symbol_id}.effects.verbs[]")
            if cleaned_verb not in seen_verbs:
                seen_verbs.add(cleaned_verb)
                verbs.append(cleaned_verb)
        completeness = str(raw_effects.get("completeness") or "")
        if completeness not in _VALID_COMPLETENESS:
            raise SymbolToolError(
                f"control {symbol_id!r} effects.completeness must be one of "
                f"{sorted(_VALID_COMPLETENESS)}, got {completeness!r}"
            )
        effects = {"verbs": verbs, "completeness": completeness}
    else:
        # Fail-safe default when L2 manifest omits effects: never guess exhaustive.
        effects = {"verbs": [], "completeness": "unknown"}

    return (
        {
            "kind": kind,
            "scope": scope,
            "locator": locator,
            "effects": effects,
        },
        effects_declared,
    )


def _parse_l2_controls(manifest_doc: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    raw_controls = manifest_doc.get("controls")
    if raw_controls is None:
        raise SymbolToolError("L2 manifest is missing required 'controls' field")

    controls: dict[str, dict[str, Any]] = {}
    effects_declared_ids: set[str] = set()

    if isinstance(raw_controls, Mapping):
        for symbol_id in sorted(raw_controls):
            entry = raw_controls[symbol_id]
            if isinstance(entry, Mapping) and "symbol_id" in entry:
                if str(entry["symbol_id"]) != str(symbol_id):
                    raise SymbolToolError(
                        f"control key {symbol_id!r} disagrees with embedded symbol_id {entry['symbol_id']!r}"
                    )
            normalized, eff_declared = _normalize_l2_control(str(symbol_id), entry)
            controls[str(symbol_id)] = normalized
            if eff_declared:
                effects_declared_ids.add(str(symbol_id))
        return controls, effects_declared_ids

    if isinstance(raw_controls, list):
        items: list[tuple[str, Mapping[str, Any]]] = []
        seen: set[str] = set()
        for idx, entry in enumerate(raw_controls):
            if not isinstance(entry, Mapping):
                raise SymbolToolError(f"controls[{idx}] must be a mapping")
            symbol_id = _require_non_empty_str(entry.get("symbol_id"), f"controls[{idx}].symbol_id")
            if symbol_id in seen:
                raise SymbolToolError(f"duplicate symbol_id in L2 manifest: {symbol_id!r}")
            seen.add(symbol_id)
            items.append((symbol_id, entry))
        for symbol_id, entry in sorted(items, key=lambda pair: pair[0]):
            normalized, eff_declared = _normalize_l2_control(symbol_id, entry)
            controls[symbol_id] = normalized
            if eff_declared:
                effects_declared_ids.add(symbol_id)
        return controls, effects_declared_ids

    raise SymbolToolError("L2 manifest 'controls' must be a list or mapping")


def import_l2_manifest(
    app_or_profile: str | AppProfile,
    manifest: str | Path | Mapping[str, Any],
    *,
    build_id: str | None = None,
    artifact_identity: Mapping[str, Any] | None = None,
    output_root: Path | None = None,
    app_dir: Path | None = None,
    package_root: Path | None = None,
    code_root: Path | None = None,
    evidence_ref: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Import a build-side L2 thin manifest into an immutable ``app-symbols.v1`` binding and claims."""
    app_name, resolved_app_dir, profile = _resolve_target_app_dir(
        app_or_profile,
        output_root=output_root,
        app_dir=app_dir,
    )
    manifest_doc, default_ref = _load_manifest_input(manifest)
    manifest_app = manifest_doc.get("app")
    if manifest_app not in (None, "") and str(manifest_app) != app_name:
        raise SymbolBindingMismatchError(
            f"L2 manifest app {manifest_app!r} does not match requested app {app_name!r}"
        )

    clean_identity = _resolve_l2_artifact_identity(
        app_name,
        profile=profile,
        manifest_doc=manifest_doc,
        build_id=build_id,
        artifact_identity=artifact_identity,
        package_root=package_root,
        code_root=code_root,
    )
    controls, effects_declared_ids = _parse_l2_controls(manifest_doc)

    symbol_doc: dict[str, Any] = {
        "schema": APP_SYMBOLS_SCHEMA,
        "app": app_name,
        "artifact_identity": clean_identity,
        "controls": controls,
    }
    loaded = _write_and_verify_symbol_binding(
        resolved_app_dir,
        app_name=app_name,
        doc=symbol_doc,
        overwrite=overwrite,
    )

    ref_str = _require_non_empty_str(evidence_ref or default_ref, "evidence_ref")
    claims: list[dict[str, Any]] = []
    for symbol_id in sorted(loaded.controls):
        control = loaded.controls[symbol_id]
        base_scope = _base_claim_scope(
            loaded,
            symbol_id=symbol_id,
            screen_id=control.screen_id,
        )
        claims.append({
            "id": _deterministic_claim_id(
                "clm-l2-loc",
                source=PROVENANCE_L2_MANIFEST,
                kind=CLAIM_KIND_LOCATOR_BINDING,
                symbol_id=symbol_id,
                artifact_content_digest=loaded.artifact_content_digest,
                binding_sha256=loaded.binding_sha256,
            ),
            "status": "active",
            "kind": CLAIM_KIND_LOCATOR_BINDING,
            "statement": (
                f"{symbol_id} binds to {control.locator.kind} "
                f"{control.locator.identifier} on {control.screen_id}"
            ),
            "evidence_level": "static_hint",
            "evidence_state": "current",
            "provenance": {"source": PROVENANCE_L2_MANIFEST},
            "scope": base_scope,
            "evidence_refs": [ref_str],
        })
        if symbol_id in effects_declared_ids:
            verbs_sorted = sorted(control.effects.verbs)
            effect_scope = {
                **base_scope,
                "verbs": verbs_sorted,
                "completeness": control.effects.completeness,
            }
            claims.append({
                "id": _deterministic_claim_id(
                    "clm-l2-eff",
                    source=PROVENANCE_L2_MANIFEST,
                    kind=CLAIM_KIND_EFFECT_SET,
                    symbol_id=symbol_id,
                    artifact_content_digest=loaded.artifact_content_digest,
                    binding_sha256=loaded.binding_sha256,
                ),
                "status": "active",
                "kind": CLAIM_KIND_EFFECT_SET,
                "statement": (
                    f"{symbol_id} declares effects verbs={verbs_sorted} "
                    f"completeness={control.effects.completeness}"
                ),
                "evidence_level": "static_hint",
                "evidence_state": "current",
                "provenance": {"source": PROVENANCE_L2_MANIFEST},
                "scope": effect_scope,
                "evidence_refs": [ref_str],
            })

    claims_path, claims_written, claims_skipped = _append_claims_batch(
        resolved_app_dir,
        app_name=app_name,
        claims=claims,
    )
    return {
        "app": app_name,
        "source": PROVENANCE_L2_MANIFEST,
        "symbol_path": str(loaded.path),
        "claims_path": str(claims_path),
        "binding_sha256": loaded.binding_sha256,
        "artifact_content_digest": loaded.artifact_content_digest,
        "artifact_kind": loaded.artifact_kind,
        "build_id": loaded.build_id,
        "control_count": len(loaded.controls),
        "claims_written": claims_written,
        "claims_skipped": claims_skipped,
    }


# ── L1 Minimal React Native main.jsbundle Blind Extractor ─────────────────────


def _unescape_js_literal(raw: str) -> str | None:
    if any(seq in raw for seq in ("\\n", "\\r", "\\t", "\\0")):
        return None
    text = raw.replace("\\/", "/").replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
    text = _JS_UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return text


def _is_valid_candidate_identifier(value: str) -> bool:
    if not value or value != value.strip():
        return False
    if len(value) < 2 or len(value) > 128:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if "://" in value or ".." in value or "//" in value:
        return False
    if _TEMPLATE_NOISE_RE.search(value):
        return False
    if not _CANDIDATE_ID_RE.fullmatch(value):
        return False
    if not any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in value):
        return False
    return True


def _find_enclosing_object_bounds(text: str, start: int, end: int, *, max_span: int = 512) -> tuple[int, int] | None:
    left_limit = max(0, start - max_span)
    depth = 0
    open_pos = -1
    for i in range(start - 1, left_limit - 1, -1):
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                open_pos = i
                break
            depth -= 1
    if open_pos == -1:
        return None

    right_limit = min(len(text), end + max_span)
    depth = 0
    close_pos = right_limit
    for i in range(end, right_limit):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                close_pos = i + 1
                break
            depth -= 1
    return open_pos, close_pos


def _top_level_object_slice(obj_text: str) -> str:
    """Keep only depth-1 content of an object literal so nested callbacks/children don't leak."""
    out: list[str] = []
    depth = 0
    for ch in obj_text:
        if ch == "{":
            depth += 1
            if depth == 1:
                out.append(ch)
        elif ch == "}":
            if depth == 1:
                out.append(ch)
            depth = max(0, depth - 1)
        elif depth <= 1:
            out.append(ch)
    return "".join(out)


def _infer_kind_from_js_context(text: str, start: int, end: int) -> str:
    bounds = _find_enclosing_object_bounds(text, start, end)
    if bounds is not None:
        open_pos, close_pos = bounds
        top_props = _top_level_object_slice(text[open_pos:close_pos])
        children_idx = top_props.find("children:")
        if children_idx != -1:
            top_props = top_props[:children_idx]
        role_match = _JS_ROLE_RE.search(top_props)
        if role_match:
            role = role_match.group(1).lower()
            if role in {"button", "tab", "link", "switch", "checkbox", "radio", "header", "search"}:
                return role
            if role in {"textbox", "textfield", "searchbox"}:
                return "input"
        prefix_window = text[max(0, open_pos - 60) : open_pos]
    else:
        window = text[max(0, start - 120) : min(len(text), end + 120)]
        role_match = _JS_ROLE_RE.search(window)
        if role_match and role_match.group(1).lower() == "button":
            return "button"
        prefix_window = text[max(0, start - 60) : start]

    if "TextInput" in prefix_window:
        return "input"
    if any(tok in prefix_window for tok in ("Pressable", "Touchable", "Button")):
        return "button"
    return "control"


def _extract_plain_js_candidates(bundle_bytes: bytes) -> list[ExtractedControlCandidate]:
    text = bundle_bytes.decode("utf-8", errors="replace")
    found: dict[str, ExtractedControlCandidate] = {}
    for match in _JS_ID_ATTR_RE.finditer(text):
        tail = text[match.end() : match.end() + 24]
        if _JS_DYNAMIC_TAIL_RE.match(tail):
            continue
        decoded = _unescape_js_literal(match.group(3))
        if decoded is None or not _is_valid_candidate_identifier(decoded):
            continue
        inferred_kind = _infer_kind_from_js_context(text, match.start(), match.end())
        existing = found.get(decoded)
        if existing is None or (existing.inferred_kind == "control" and inferred_kind != "control"):
            found[decoded] = ExtractedControlCandidate(
                identifier=decoded,
                attribute=match.group(1),
                inferred_kind=inferred_kind,
            )
    return [found[key] for key in sorted(found)]


def _is_hermes_dotted_control_id(token: str) -> bool:
    if not _is_valid_candidate_identifier(token):
        return False
    if not _HERMES_DOTTED_CONTROL_RE.fullmatch(token):
        return False
    parts = token.split(".")
    if parts[0] in _HERMES_NOISE_PREFIXES:
        return False
    if parts[-1] in _HERMES_NOISE_EXTENSIONS:
        return False
    return True


def _extract_hermes_candidates(bundle_bytes: bytes) -> list[ExtractedControlCandidate]:
    found: dict[str, ExtractedControlCandidate] = {}

    # 1. Explicit adjacent marker pairs (e.g. testID\x00home.ai_prompt.preferences\x00).
    for match in _HERMES_ADJACENT_RE.finditer(bundle_bytes):
        raw = match.group(1).decode("ascii", errors="ignore")
        if _is_valid_candidate_identifier(raw) and raw not in {"testID", "accessibilityIdentifier"}:
            found.setdefault(
                raw,
                ExtractedControlCandidate(
                    identifier=raw,
                    attribute="hermes_adjacent",
                    inferred_kind="control",
                ),
            )

    # 2. High-precision lowercase dotted identifiers in the Hermes string pool.
    for match in _HERMES_PRINTABLE_TOKEN_RE.finditer(bundle_bytes):
        token = match.group(0).decode("ascii", errors="ignore")
        if _is_hermes_dotted_control_id(token):
            found.setdefault(
                token,
                ExtractedControlCandidate(
                    identifier=token,
                    attribute="hermes_string_table",
                    inferred_kind="control",
                ),
            )

    return [found[key] for key in sorted(found)]


def extract_rn_identifiers_from_bundle(bundle_bytes: bytes) -> BundleExtractionResult:
    """Extract deterministic candidate control identifiers from a React Native ``main.jsbundle``."""
    if not isinstance(bundle_bytes, (bytes, bytearray)) or not bundle_bytes:
        raise SymbolToolError("main.jsbundle content must be non-empty bytes")
    raw = bytes(bundle_bytes)
    bundle_sha256 = hashlib.sha256(raw).hexdigest()

    if raw.startswith(HERMES_BYTECODE_MAGIC):
        bundle_format: BundleFormat = "hermes_bytecode"
        candidates = _extract_hermes_candidates(raw)
    else:
        bundle_format = "plain_js"
        candidates = _extract_plain_js_candidates(raw)
    return BundleExtractionResult(
        bundle_format=bundle_format,
        main_jsbundle_sha256=bundle_sha256,
        candidates=tuple(candidates),
    )


def _identifier_to_symbol_id(identifier: str, *, prefix: str, used: set[str]) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._:-]+", "_", identifier)
    sanitized = re.sub(r"_+", "_", sanitized).strip("._:-")
    if not sanitized or not re.match(r"^[A-Za-z0-9]", sanitized):
        sanitized = f"id_{hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:8]}"

    clean_prefix = prefix.strip().rstrip(".")
    base = f"{clean_prefix}.{sanitized}" if clean_prefix else sanitized
    if base in _FORBIDDEN_SYMBOL_IDS or not _SYMBOL_ID_RE.fullmatch(base):
        base = f"ctrl.rn.{sanitized}"

    candidate = base
    if candidate in used:
        suffix = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:8]
        candidate = f"{base}.{suffix}"
    used.add(candidate)
    return candidate


def _read_ipa_metadata_and_bundle(
    ipa_path: Path,
) -> tuple[str, dict[str, str], bytes, str]:
    """Read raw IPA bytes once, extract Info.plist metadata and ``main.jsbundle`` bytes."""
    if not ipa_path.is_file():
        raise SymbolToolError(f"IPA file not found: {ipa_path}")
    try:
        ipa_bytes = ipa_path.read_bytes()
    except OSError as exc:
        raise SymbolToolError(f"failed to read IPA {ipa_path}: {exc}") from exc
    ipa_sha256 = hashlib.sha256(ipa_bytes).hexdigest()

    try:
        archive = zipfile.ZipFile(io.BytesIO(ipa_bytes))
    except zipfile.BadZipFile as exc:
        raise SymbolToolError(f"invalid IPA zip archive {ipa_path}: {exc}") from exc

    with archive:
        plist_entries = [
            entry
            for entry in archive.infolist()
            if len(entry.filename.split("/")) == 3
            and entry.filename.startswith("Payload/")
            and entry.filename.split("/")[1].endswith(".app")
            and entry.filename.split("/")[-1] == "Info.plist"
        ]
        if len(plist_entries) != 1:
            raise SymbolToolError(
                f"IPA {ipa_path.name} has missing or ambiguous Payload/*.app/Info.plist "
                f"(found {len(plist_entries)})"
            )
        app_bundle_dir = plist_entries[0].filename.split("/")[1]
        try:
            plist_doc = plistlib.loads(archive.read(plist_entries[0]))
        except Exception as exc:  # noqa: BLE001
            raise SymbolToolError(f"failed to parse Info.plist in {ipa_path.name}: {exc}") from exc
        if not isinstance(plist_doc, Mapping):
            raise SymbolToolError(f"Info.plist in {ipa_path.name} is not a mapping")

        meta = {
            "bundle_id": _require_non_empty_str(
                plist_doc.get("CFBundleIdentifier"), "Info.plist.CFBundleIdentifier"
            ),
            "short_version": _require_non_empty_str(
                plist_doc.get("CFBundleShortVersionString"),
                "Info.plist.CFBundleShortVersionString",
            ),
            "build_version": _require_non_empty_str(
                plist_doc.get("CFBundleVersion"), "Info.plist.CFBundleVersion"
            ),
        }

        bundle_member = f"Payload/{app_bundle_dir}/main.jsbundle"
        try:
            bundle_bytes = archive.read(bundle_member)
        except KeyError as exc:
            raise SymbolToolError(
                f"main.jsbundle not found in IPA {ipa_path.name} (expected {bundle_member})"
            ) from exc

    return ipa_sha256, meta, bundle_bytes, f"{ipa_path.name}::{bundle_member}"


def extract_l1_rn_symbols(
    app_or_profile: str | AppProfile,
    *,
    ipa_path: str | Path | None = None,
    bundle_path: str | Path | None = None,
    build_id: str | None = None,
    artifact_identity: Mapping[str, Any] | None = None,
    default_screen_id: str = DEFAULT_L1_SCREEN_ID,
    symbol_prefix: str = DEFAULT_L1_SYMBOL_PREFIX,
    output_root: Path | None = None,
    app_dir: Path | None = None,
    evidence_ref: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Blind-extract React Native ``testID`` / ``accessibilityIdentifier`` candidates into draft ``app-symbols.v1``.

    Always marks every extracted control with ``effects: {verbs: [], completeness: "unknown"}``
    and records ``l1_ipa_extractor`` (``static_hint``) claims in ``apps/<app>/symbols/claims.json``.
    """
    app_name, resolved_app_dir, profile = _resolve_target_app_dir(
        app_or_profile,
        output_root=output_root,
        app_dir=app_dir,
    )
    screen_id = _require_non_empty_str(default_screen_id, "default_screen_id")

    if (ipa_path is None) == (bundle_path is None):
        raise SymbolToolError("provide exactly one of ipa_path or bundle_path")

    if ipa_path is not None:
        resolved_ipa = Path(ipa_path)
        ipa_sha256, meta, bundle_bytes, default_ref = _read_ipa_metadata_and_bundle(resolved_ipa)
        if artifact_identity is not None:
            clean_identity = _strip_and_normalize_explicit_identity(
                artifact_identity, profile=profile
            )
            if clean_identity["kind"] == "ipa_build" and clean_identity["artifact_sha256"] != ipa_sha256:
                raise SymbolBindingMismatchError(
                    f"explicit artifact_sha256 {clean_identity['artifact_sha256']} "
                    f"does not match IPA bytes sha256 {ipa_sha256}"
                )
        else:
            resolved_build_id = (
                _require_non_empty_str(build_id, "build_id")
                if build_id is not None
                else f"bld-{ipa_sha256[:12]}"
            )
            clean_identity = {
                "kind": "ipa_build",
                "bundle_id": meta["bundle_id"],
                "short_version": meta["short_version"],
                "build_version": meta["build_version"],
                "build_id": resolved_build_id,
                "artifact_sha256": ipa_sha256,
            }
    else:
        assert bundle_path is not None
        if artifact_identity is None:
            raise SymbolToolError(
                "standalone bundle_path extraction requires an explicit rn_candidate artifact_identity"
            )
        if build_id is not None:
            raise SymbolBindingMismatchError(
                "build_id cannot be combined with standalone rn_candidate bundle extraction"
            )
        resolved_bundle = Path(bundle_path)
        if not resolved_bundle.is_file():
            raise SymbolToolError(f"main.jsbundle file not found: {resolved_bundle}")
        try:
            bundle_bytes = resolved_bundle.read_bytes()
        except OSError as exc:
            raise SymbolToolError(f"failed to read bundle {resolved_bundle}: {exc}") from exc
        clean_identity = _strip_and_normalize_explicit_identity(artifact_identity, profile=profile)
        if clean_identity["kind"] != "rn_candidate":
            raise SymbolToolError(
                "standalone bundle_path extraction requires artifact_identity.kind == 'rn_candidate'"
            )
        actual_bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        if clean_identity["main_jsbundle_sha256"] != actual_bundle_sha:
            raise SymbolBindingMismatchError(
                f"bundle sha256 {actual_bundle_sha} does not match "
                f"artifact_identity.main_jsbundle_sha256 {clean_identity['main_jsbundle_sha256']}"
            )
        default_ref = resolved_bundle.name

    extraction = extract_rn_identifiers_from_bundle(bundle_bytes)
    if (
        clean_identity["kind"] == "rn_candidate"
        and clean_identity["main_jsbundle_sha256"] != extraction.main_jsbundle_sha256
    ):
        raise SymbolBindingMismatchError(
            f"main.jsbundle sha256 {extraction.main_jsbundle_sha256} does not match "
            f"artifact_identity.main_jsbundle_sha256 {clean_identity['main_jsbundle_sha256']}"
        )

    controls: dict[str, dict[str, Any]] = {}
    used_symbol_ids: set[str] = set()
    for candidate in extraction.candidates:
        symbol_id = _identifier_to_symbol_id(
            candidate.identifier,
            prefix=symbol_prefix,
            used=used_symbol_ids,
        )
        controls[symbol_id] = {
            "kind": candidate.inferred_kind,
            "scope": {
                "screen_id": screen_id,
            },
            "locator": {
                "kind": "ax_identifier",
                "identifier": candidate.identifier,
                "role": None,
                "enabled_only": True,
                "require_unique": True,
            },
            "effects": {
                "verbs": [],
                "completeness": "unknown",
            },
        }

    symbol_doc: dict[str, Any] = {
        "schema": APP_SYMBOLS_SCHEMA,
        "app": app_name,
        "artifact_identity": clean_identity,
        "controls": controls,
    }
    loaded = _write_and_verify_symbol_binding(
        resolved_app_dir,
        app_name=app_name,
        doc=symbol_doc,
        overwrite=overwrite,
    )

    ref_str = _require_non_empty_str(evidence_ref or default_ref, "evidence_ref")
    claims: list[dict[str, Any]] = []
    for symbol_id in sorted(loaded.controls):
        control = loaded.controls[symbol_id]
        scope = {
            **_base_claim_scope(
                loaded,
                symbol_id=symbol_id,
                screen_id=control.screen_id,
            ),
            "bundle_format": extraction.bundle_format,
            "main_jsbundle_sha256": extraction.main_jsbundle_sha256,
        }
        claims.append({
            "id": _deterministic_claim_id(
                "clm-l1-loc",
                source=PROVENANCE_L1_EXTRACTOR,
                kind=CLAIM_KIND_LOCATOR_BINDING,
                symbol_id=symbol_id,
                artifact_content_digest=loaded.artifact_content_digest,
                binding_sha256=loaded.binding_sha256,
            ),
            "status": "draft",
            "kind": CLAIM_KIND_LOCATOR_BINDING,
            "statement": (
                f"{symbol_id} candidate ax_identifier {control.locator.identifier} "
                f"extracted from {extraction.bundle_format} main.jsbundle"
            ),
            "evidence_level": "static_hint",
            "evidence_state": "current",
            "provenance": {"source": PROVENANCE_L1_EXTRACTOR},
            "scope": scope,
            "evidence_refs": [ref_str],
        })

    claims_path, claims_written, claims_skipped = _append_claims_batch(
        resolved_app_dir,
        app_name=app_name,
        claims=claims,
    )
    return {
        "app": app_name,
        "source": PROVENANCE_L1_EXTRACTOR,
        "bundle_format": extraction.bundle_format,
        "symbol_path": str(loaded.path),
        "claims_path": str(claims_path),
        "binding_sha256": loaded.binding_sha256,
        "artifact_content_digest": loaded.artifact_content_digest,
        "artifact_kind": loaded.artifact_kind,
        "build_id": loaded.build_id,
        "bundle_id": loaded.bundle_id,
        "short_version": loaded.short_version,
        "build_version": loaded.build_version,
        "main_jsbundle_sha256": extraction.main_jsbundle_sha256,
        "control_count": len(loaded.controls),
        "extracted_identifiers": extraction.identifiers,
        "claims_written": claims_written,
        "claims_skipped": claims_skipped,
    }


# ── Dual-Denominator Coverage Calculator (§4.4) ───────────────────────────────


def _clean_id_set(values: Iterable[str]) -> frozenset[str]:
    cleaned: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text:
            cleaned.add(text)
    return frozenset(cleaned)


def compute_dual_denominator_metrics(
    extracted_identifiers: Iterable[str],
    unique_hit_identifiers: Iterable[str],
    target_identifiers: Iterable[str],
) -> dict[str, Any]:
    """Compute §4.4 dual-denominator L1 coverage metrics.

    - ``candidate_usability_rate = len(extracted & unique_hits) / len(extracted)``
    - ``business_target_coverage = len(extracted & unique_hits & targets) / len(targets)``
    """
    extracted = _clean_id_set(extracted_identifiers)
    unique_hits = _clean_id_set(unique_hit_identifiers)
    targets = _clean_id_set(target_identifiers)

    usable_candidates = extracted & unique_hits
    covered_targets = usable_candidates & targets

    extracted_total = len(extracted)
    unique_hits_total = len(usable_candidates)
    target_total = len(targets)
    covered_target_count = len(covered_targets)

    return {
        "extracted_total": extracted_total,
        "unique_hits_total": unique_hits_total,
        "target_total": target_total,
        "covered_target_count": covered_target_count,
        "candidate_usability_rate": (
            unique_hits_total / extracted_total if extracted_total > 0 else 0.0
        ),
        "business_target_coverage": (
            covered_target_count / target_total if target_total > 0 else 0.0
        ),
        "usable_candidates": sorted(usable_candidates),
        "unmatched_candidates": sorted(extracted - usable_candidates),
        "covered_targets": sorted(covered_targets),
        "missed_targets": sorted(targets - covered_targets),
    }


__all__ = [
    "BundleExtractionResult",
    "BundleFormat",
    "DEFAULT_L1_SCREEN_ID",
    "DEFAULT_L1_SYMBOL_PREFIX",
    "ExtractedControlCandidate",
    "HERMES_BYTECODE_MAGIC",
    "PROVENANCE_L1_EXTRACTOR",
    "PROVENANCE_L2_MANIFEST",
    "SymbolToolError",
    "compute_dual_denominator_metrics",
    "extract_l1_rn_symbols",
    "extract_rn_identifiers_from_bundle",
    "import_l2_manifest",
]
