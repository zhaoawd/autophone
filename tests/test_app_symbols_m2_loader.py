"""Unit & contract tests for Milestone M2: symbol binding loader, artifact routing,
external claims, ax_backend end-to-end plumbing, ax_checkpoint v2, and run-result schema.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.assets.app_symbols import (
    CLAIM_KIND_UNIQUE_VISIBILITY,
    LoadedSymbolBinding,
    SymbolBindingAmbiguousError,
    SymbolBindingError,
    SymbolBindingMismatchError,
    SymbolBindingMissingError,
    SymbolClaimError,
    SymbolControlSpec,
    SymbolEffectSpec,
    SymbolLocatorSpec,
    artifact_filename_for_digest,
    canonical_artifact_content_digest,
    canonical_ipa_content_digest,
    canonical_rn_candidate_content_digest,
    has_reusable_unique_visibility,
    load_symbol_binding,
    load_symbol_binding_file,
    load_symbol_binding_for_app,
    load_symbol_claims,
    record_unique_visibility_claim,
    resolve_artifact_identity_for_app,
    save_symbol_claims,
    symbol_claims_path,
    symbols_artifacts_dir,
)
from autophone.assets.apps import AppProfile
from autophone.contracts import ContractError, validate_or_raise
from autophone.device.ax_session import (
    AX_BACKEND_UNSUPPORTED_CODE,
    AxSession,
    AxUnavailable,
    UnsupportedAxBackendError,
)
from autophone.device.ax_target import AxError, load_dump, parse_dump
from autophone.observation import ax_checkpoint
from autophone.observation.capture import (
    LazyAxSession,
    ax_scope,
    build_ax_session,
    build_run_ax_session,
    ensure_ax_backend_supported,
    lazy_ax_scope,
    wait_for_spawn_ready,
)
from autophone.observation.run_result import RunResultAssembler, phase2_result_from_json

SHA_V1 = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
SHA_V2 = "25393f786474b0e85472d0c6e35edef572759ef3b24d0abb5599e1bbe5079e69"
SHA_JS_1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_JS_2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHA_ASSETS = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
SHA_RIG_A = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
SHA_RIG_B = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
SHA_OTHER = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"


# ── Fixture Helpers ───────────────────────────────────────────────────────────


def _make_profile(
    tmp_path: Path,
    *,
    app: str = "food_ordering",
    bundle_id: str = "com.autophone.foodorderingdemo.v2",
    ax_enabled: bool = True,
    ax_backend: str | None = "frida_inproc",
    extra_caps: dict[str, Any] | None = None,
) -> AppProfile:
    raw: dict[str, Any] = {
        "schema_version": "v1",
        "app": app,
        "bundle_id": bundle_id,
        "display_name": "Food Ordering",
    }
    caps: dict[str, Any] = dict(extra_caps or {})
    ax_cfg: dict[str, Any] = {"enabled": ax_enabled}
    if ax_backend is not None:
        ax_cfg["backend"] = ax_backend
    caps["ax"] = ax_cfg
    raw["capabilities"] = caps
    app_dir = tmp_path / "apps" / app
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "profile.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return AppProfile(raw=raw, root=tmp_path, state_root=tmp_path)


def _ipa_symbols_doc(
    *,
    app: str = "food_ordering",
    build_id: str = "bld-14282f675363",
    artifact_sha256: str = SHA_V1,
    identifier: str = "home.ai_prompt.preferences",
    screen_id: str = "S01_home",
    short_version: str = "1.0.0",
    build_version: str = "1",
) -> dict[str, Any]:
    return {
        "schema": "autophone.app-symbols/1",
        "app": app,
        "artifact_identity": {
            "kind": "ipa_build",
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": short_version,
            "build_version": build_version,
            "build_id": build_id,
            "artifact_sha256": artifact_sha256,
        },
        "controls": {
            "ctrl.home.preferences": {
                "kind": "button",
                "scope": {
                    "screen_id": screen_id,
                    "region": [0.05, 0.15, 0.95, 0.90],
                },
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": identifier,
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {
                    "verbs": [],
                    "completeness": "exhaustive",
                },
            }
        },
    }


def _rn_symbols_doc(
    *,
    app: str = "food_ordering",
    base_ipa_sha256: str = SHA_V1,
    main_jsbundle_sha256: str = SHA_JS_1,
    assets_tree_sha256: str = SHA_ASSETS,
    identifier: str = "home.ai_prompt.preferences.rn",
) -> dict[str, Any]:
    return {
        "schema": "autophone.app-symbols/1",
        "app": app,
        "artifact_identity": {
            "kind": "rn_candidate",
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
            "base_ipa_sha256": base_ipa_sha256,
            "main_jsbundle_sha256": main_jsbundle_sha256,
            "assets_tree_sha256": assets_tree_sha256,
        },
        "controls": {
            "ctrl.home.preferences": {
                "kind": "button",
                "scope": {"screen_id": "S01_home"},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": identifier,
                    "role": None,
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {
                    "verbs": ["place_order", "pay"],
                    "completeness": "lower_bound",
                },
            }
        },
    }


def _write_yaml(path: Path, doc: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _write_v2_package_manifest(
    root: Path,
    *,
    app: str = "food_ordering",
    builds: list[dict[str, Any]],
    default_slot: str | None = "001",
) -> None:
    manifest = {
        "schema_version": "v2",
        "app": app,
        "bundle_id": "com.autophone.foodorderingdemo.v2",
        "default_slot": default_slot,
        "version_slots": [
            {
                "slot": b["slot"],
                "short_version": b.get("short_version", "1.0.0"),
                "candidate_build_id": b["build_id"],
                "created_at": "2026-11-26T10:00:00Z",
            }
            for b in builds
        ],
        "builds": [
            {
                "build_id": b["build_id"],
                "slot": b["slot"],
                "label": b.get("label"),
                "bundle_id": "com.autophone.foodorderingdemo.v2",
                "build_version": b.get("build_version", "1"),
                "source_revision": {"kind": "unknown", "state": "unknown"},
                "artifact": {
                    "file": f"{b['build_id']}.ipa",
                    "sha256": b["sha256"],
                    "size": 1024,
                },
                "artifact_availability": "present",
                "release_like": True,
                "release_evidence": "embedded_js_bundle:Payload/App.app/main.jsbundle",
                "legacy_slot": None,
                "added_at": "2026-11-26T10:00:00Z",
            }
            for b in builds
        ],
    }
    manifest_path = root / "packages" / app / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _build_identity_v2(
    *,
    identity_state: str = "confirmed",
    target_fingerprint: str = f"sha256:{SHA_RIG_A}",
    with_candidate: bool = True,
    base_ipa_sha256: str = SHA_V1,
    main_jsbundle_sha256: str = SHA_JS_1,
    assets_tree_sha256: str = SHA_ASSETS,
    matched_registry_sha256: str | None = SHA_V1,
) -> dict[str, Any]:
    candidate: dict[str, Any] | None = None
    if with_candidate:
        candidate = {
            "target_fingerprint": target_fingerprint,
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "base_build_id": "bld-14282f675363",
            "base_ipa_sha256": base_ipa_sha256,
            "native_runtime_id": SHA_OTHER,
            "update_source": "ota",
            "active_update_id": "upd-20261126-01",
            "main_jsbundle_sha256": main_jsbundle_sha256,
            "assets_tree_sha256": assets_tree_sha256,
            "evidence_role": "identity_only",
            "candidate_kind": "rn_update",
        }
    return {
        "schema_version": "v2",
        "run_id": "run_20261126_01",
        "app": "food_ordering",
        "identity_state": identity_state,
        "identity_source": "combined",
        "subject_build_key": "build-v1:" + SHA_V1,
        "device_observation": {
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
        },
        "match": {
            "state": "exact" if identity_state == "confirmed" else identity_state,
            "matched_registry_sha256": matched_registry_sha256,
        },
        "subject_epochs": [
            {
                "epoch_ref": "epoch-1",
                "pid": 4242,
                "bundle_id": "com.autophone.foodorderingdemo.v2",
                "short_version": "1.0.0",
                "build_version": "1",
                "started_at": "2026-11-26T12:00:00Z",
                "source": "rn_candidate" if with_candidate else "uds_info_plist",
                "superseded_at": None,
                **(
                    {
                        "candidate_identity_ref": f"candidate-identities/{main_jsbundle_sha256}.json",
                        "candidate_identity_sha256": main_jsbundle_sha256,
                        "candidate_identity": candidate,
                    }
                    if candidate is not None
                    else {}
                ),
            }
        ],
        "updated_at": "2026-11-26T12:00:00Z",
        "candidate_identity": candidate,
    }


# ── 1. Canonical Pure-Content Digest (Shared Contract with M5) ────────────────


def test_canonical_artifact_content_digest_ipa_and_rn_strips_target_fingerprint() -> None:
    assert canonical_ipa_content_digest(SHA_V1.upper()) == SHA_V1
    assert canonical_artifact_content_digest(
        {"kind": "ipa_build", "artifact_sha256": SHA_V1.upper()}
    ) == SHA_V1

    expected_rn = hashlib.sha256(
        json.dumps(
            {
                "kind": "rn_candidate",
                "base_ipa_sha256": SHA_V1,
                "main_jsbundle_sha256": SHA_JS_1,
                "assets_tree_sha256": SHA_ASSETS,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert (
        canonical_rn_candidate_content_digest(
            base_ipa_sha256=SHA_V1.upper(),
            main_jsbundle_sha256=SHA_JS_1.upper(),
            assets_tree_sha256=SHA_ASSETS.upper(),
        )
        == expected_rn
    )

    # Identical RN content on two different Rigs (different target_fingerprint) MUST yield identical digest.
    rig_a_cand = _build_identity_v2(target_fingerprint=f"sha256:{SHA_RIG_A}")["candidate_identity"]
    rig_b_cand = _build_identity_v2(target_fingerprint=f"sha256:{SHA_RIG_B}")["candidate_identity"]
    assert canonical_artifact_content_digest(rig_a_cand) == expected_rn
    assert canonical_artifact_content_digest(rig_b_cand) == expected_rn

    # Changing main_jsbundle_sha256 changes the digest even with same version triplet.
    diff_js = _build_identity_v2(main_jsbundle_sha256=SHA_JS_2)["candidate_identity"]
    assert canonical_artifact_content_digest(diff_js) != expected_rn

    # Rejects malformed digests and mixed identities.
    with pytest.raises(SymbolBindingError):
        canonical_artifact_content_digest({"kind": "ipa_build", "artifact_sha256": "deadbeef"})
    with pytest.raises(SymbolBindingError):
        canonical_artifact_content_digest(
            {
                "kind": "ipa_build",
                "artifact_sha256": SHA_V1,
                "main_jsbundle_sha256": SHA_JS_1,
            }
        )
    with pytest.raises(SymbolBindingError):
        canonical_artifact_content_digest({"kind": "unknown_kind", "artifact_sha256": SHA_V1})


# ── 2. Multi-Version Routing, Single-Read Hashing, and Fail-Closed Errors ─────


def test_multi_version_artifact_routing_ipa_and_rn_candidate(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    artifacts = symbols_artifacts_dir(profile)

    doc_v1 = _ipa_symbols_doc(
        build_id="bld-14282f675363",
        artifact_sha256=SHA_V1,
        identifier="home.pref.v1",
    )
    doc_v2 = _ipa_symbols_doc(
        build_id="bld-25393f786474",
        artifact_sha256=SHA_V2,
        identifier="home.pref.v2",
    )
    doc_rn = _rn_symbols_doc(
        base_ipa_sha256=SHA_V1,
        main_jsbundle_sha256=SHA_JS_1,
        assets_tree_sha256=SHA_ASSETS,
        identifier="home.pref.rn",
    )
    rn_digest = canonical_artifact_content_digest(doc_rn["artifact_identity"])

    raw_v1 = _write_yaml(artifacts / artifact_filename_for_digest(SHA_V1), doc_v1)
    raw_v2 = _write_yaml(artifacts / artifact_filename_for_digest(SHA_V2, full=True), doc_v2)
    raw_rn = _write_yaml(artifacts / "rn-candidate-custom.yaml", doc_rn)

    loaded_v1 = load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    assert isinstance(loaded_v1, LoadedSymbolBinding)
    assert loaded_v1.binding_sha256 == hashlib.sha256(raw_v1).hexdigest()
    assert loaded_v1.artifact_content_digest == SHA_V1
    assert loaded_v1.artifact_kind == "ipa_build"
    assert loaded_v1.build_id == "bld-14282f675363"
    ctrl = loaded_v1.require_control("ctrl.home.preferences")
    assert isinstance(ctrl, SymbolControlSpec)
    assert isinstance(ctrl.locator, SymbolLocatorSpec)
    assert isinstance(ctrl.effects, SymbolEffectSpec)
    assert ctrl.locator.identifier == "home.pref.v1"
    assert ctrl.scope.screen_id == "S01_home"
    assert ctrl.region == (0.05, 0.15, 0.95, 0.90)
    assert ctrl.effects.verbs == frozenset()
    assert ctrl.effects.completeness == "exhaustive"

    loaded_v2 = load_symbol_binding(
        profile,
        artifact_content_digest=SHA_V2,
        expected_build_id="bld-25393f786474",
    )
    assert loaded_v2.binding_sha256 == hashlib.sha256(raw_v2).hexdigest()
    assert loaded_v2.require_control("ctrl.home.preferences").locator.identifier == "home.pref.v2"

    loaded_rn = load_symbol_binding(profile, artifact_content_digest=rn_digest)
    assert loaded_rn.binding_sha256 == hashlib.sha256(raw_rn).hexdigest()
    assert loaded_rn.artifact_kind == "rn_candidate"
    assert loaded_rn.build_id is None
    rn_ctrl = loaded_rn.require_control("ctrl.home.preferences")
    assert rn_ctrl.locator.identifier == "home.pref.rn"
    assert rn_ctrl.effects.verbs == frozenset({"place_order", "pay"})
    assert rn_ctrl.effects.completeness == "lower_bound"

    # Immutability verification
    with pytest.raises(FrozenInstanceError):
        loaded_v1.app = "tampered"  # type: ignore[misc]
    with pytest.raises(TypeError):
        loaded_v1.controls["new"] = ctrl  # type: ignore[index]
    with pytest.raises(TypeError):
        loaded_v1.artifact_identity["build_id"] = "tampered"  # type: ignore[index]


def test_routing_fails_closed_on_missing_duplicate_tampered_or_mismatched(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    artifacts = symbols_artifacts_dir(profile)

    # Missing directory -> SymbolBindingMissingError
    with pytest.raises(SymbolBindingMissingError):
        load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    # Tampered internal digest inside <SHA_V1[:12]>.yaml -> SymbolBindingMismatchError
    tampered = _ipa_symbols_doc(build_id="bld-25393f786474", artifact_sha256=SHA_V2)
    tampered_path = artifacts / f"{SHA_V1[:12]}.yaml"
    _write_yaml(tampered_path, tampered)
    with pytest.raises(SymbolBindingMismatchError):
        load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    # Loading SHA_V2 also fails closed because filename stem contradicts SHA_V2!
    with pytest.raises(SymbolBindingMismatchError):
        load_symbol_binding(profile, artifact_content_digest=SHA_V2)
    tampered_path.unlink()

    # Duplicate files matching same digest -> SymbolBindingAmbiguousError
    doc_v1 = _ipa_symbols_doc(artifact_sha256=SHA_V1)
    _write_yaml(artifacts / f"{SHA_V1[:12]}.yaml", doc_v1)
    _write_yaml(artifacts / f"{SHA_V1}.yaml", doc_v1)
    with pytest.raises(SymbolBindingAmbiguousError):
        load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    (artifacts / f"{SHA_V1}.yaml").unlink()

    # App mismatch -> SymbolBindingMismatchError
    with pytest.raises(SymbolBindingMismatchError):
        load_symbol_binding(profile, artifact_content_digest=SHA_V1, expected_app="other_app")

    # Build ID mismatch -> SymbolBindingMismatchError
    with pytest.raises(SymbolBindingMismatchError):
        load_symbol_binding(
            profile,
            artifact_content_digest=SHA_V1,
            expected_build_id="bld-000000000000",
        )

    # Schema violation (embedding target_fingerprint) -> SymbolBindingError
    bad_path = tmp_path / "bad.yaml"
    bad_doc = _ipa_symbols_doc(artifact_sha256=SHA_V1)
    bad_doc["artifact_identity"]["target_fingerprint"] = f"sha256:{SHA_RIG_A}"
    _write_yaml(bad_path, bad_doc)
    with pytest.raises(SymbolBindingError):
        load_symbol_binding_file(bad_path)


def test_resolve_artifact_identity_for_app_modes(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    _write_v2_package_manifest(
        tmp_path,
        builds=[
            {"slot": "001", "build_id": "bld-14282f675363", "sha256": SHA_V1, "short_version": "1.0.0"},
            {"slot": "002", "build_id": "bld-25393f786474", "sha256": SHA_V2, "short_version": "1.0.0"},
        ],
        default_slot="001",
    )
    artifacts = symbols_artifacts_dir(profile)
    _write_yaml(artifacts / f"{SHA_V1[:12]}.yaml", _ipa_symbols_doc(build_id="bld-14282f675363", artifact_sha256=SHA_V1))
    _write_yaml(artifacts / f"{SHA_V2[:12]}.yaml", _ipa_symbols_doc(build_id="bld-25393f786474", artifact_sha256=SHA_V2))
    rn_doc = _rn_symbols_doc(base_ipa_sha256=SHA_V1, main_jsbundle_sha256=SHA_JS_1, assets_tree_sha256=SHA_ASSETS)
    rn_digest = canonical_artifact_content_digest(rn_doc["artifact_identity"])
    _write_yaml(artifacts / f"{rn_digest[:12]}.yaml", rn_doc)

    # 1. Explicit --build-id resolves exact build 2
    resolved_explicit = resolve_artifact_identity_for_app(profile, build_id="bld-25393f786474")
    assert resolved_explicit.source == "explicit_build_id"
    assert resolved_explicit.artifact_content_digest == SHA_V2
    binding_v2 = load_symbol_binding_for_app(profile, build_id="bld-25393f786474")
    assert binding_v2.build_id == "bld-25393f786474"

    # 2. Offline default / promoted build resolves default_slot=001 without modifying profile.yaml
    resolved_default = resolve_artifact_identity_for_app(profile)
    assert resolved_default.source == "package_store_default"
    assert resolved_default.artifact_content_digest == SHA_V1
    binding_v1 = load_symbol_binding_for_app(profile)
    assert binding_v1.build_id == "bld-14282f675363"

    # 3. Confirmed build-identity.v2.json (RN candidate) strips target_fingerprint and routes to RN symbols
    run_dir = tmp_path / "run_rn"
    run_dir.mkdir()
    (run_dir / "build-identity.json").write_text(
        json.dumps(_build_identity_v2(identity_state="confirmed", with_candidate=True)),
        encoding="utf-8",
    )
    resolved_rn = resolve_artifact_identity_for_app(profile, run_dir=run_dir)
    assert resolved_rn.kind == "rn_candidate"
    assert resolved_rn.artifact_content_digest == rn_digest
    assert "target_fingerprint" not in resolved_rn.to_artifact_identity()
    loaded_rn = load_symbol_binding_for_app(profile, run_dir=run_dir)
    assert loaded_rn.artifact_content_digest == rn_digest

    # 4. Confirmed ipa_build with conflicting package_snapshot SHA fails closed
    confirmed_ipa_doc = _build_identity_v2(
        identity_state="confirmed", with_candidate=False, matched_registry_sha256=SHA_V1
    )
    resolved_ipa_run = resolve_artifact_identity_for_app(
        profile,
        build_identity_doc=confirmed_ipa_doc,
        package_snapshot={
            "registry_build_ref": {
                "app": "food_ordering",
                "build_id": "bld-14282f675363",
                "artifact_sha256": SHA_V1,
            }
        },
    )
    assert resolved_ipa_run.kind == "ipa_build"
    assert resolved_ipa_run.artifact_content_digest == SHA_V1
    assert resolved_ipa_run.build_id == "bld-14282f675363"

    with pytest.raises(SymbolBindingMismatchError):
        resolve_artifact_identity_for_app(
            profile,
            build_identity_doc=confirmed_ipa_doc,
            package_snapshot={
                "registry_build_ref": {
                    "app": "food_ordering",
                    "build_id": "bld-25393f786474",
                    "artifact_sha256": SHA_V2,
                }
            },
        )

    # Direct routing via build-identity.v2 candidate_identity strips target_fingerprint
    rn_cand = _build_identity_v2(target_fingerprint=f"sha256:{SHA_RIG_B}")["candidate_identity"]
    loaded_by_cand = load_symbol_binding(profile, artifact_identity=rn_cand)
    assert loaded_by_cand.artifact_content_digest == rn_digest

    # 5. Non-confirmed build-identity.json MUST fail closed!
    for bad_state in ("ambiguous", "mismatch", "unavailable", "version_only"):
        bad_doc = _build_identity_v2(identity_state=bad_state, with_candidate=False)
        with pytest.raises(SymbolBindingMismatchError):
            resolve_artifact_identity_for_app(profile, build_identity_doc=bad_doc)


# ── 3. External Claims Store & Reusable unique_visibility Scope ───────────────


def test_external_claims_keep_binding_sha256_constant_and_enforce_scope(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    artifacts = symbols_artifacts_dir(profile)
    sym_path = artifacts / f"{SHA_V1[:12]}.yaml"
    raw_before = _write_yaml(sym_path, _ipa_symbols_doc(artifact_sha256=SHA_V1))
    binding_before = load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    # Empty claims before any observation
    empty = load_symbol_claims(profile)
    assert empty == {"schema_version": "v1", "app": "food_ordering", "claims": []}
    assert not has_reusable_unique_visibility(
        empty,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )

    # Non-confirmed identity states MUST be rejected without mutating claims.json
    for rejected_state in ("ambiguous", "mismatch", "unavailable", "version_only", None, ""):
        with pytest.raises(SymbolClaimError):
            record_unique_visibility_claim(
                profile,
                symbol_id="ctrl.home.preferences",
                artifact_content_digest=SHA_V1,
                binding_sha256=binding_before.binding_sha256,
                screen_id="S01_home",
                identity_state=rejected_state,
                ax_backend="frida_inproc",
                evidence_refs=["runs/run_01/build-identity.json"],
            )
    assert not symbol_claims_path(profile).exists()

    # Confirmed vm_observation writes to external claims.json without touching symbol YAML
    claim = record_unique_visibility_claim(
        profile,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        artifact_sha256=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        identity_state="confirmed",
        ax_backend="frida_inproc",
        evidence_refs=["runs/run_01/build-identity.json", "runs/run_01/shots/R3_01.ax.json"],
    )
    assert claim["kind"] == CLAIM_KIND_UNIQUE_VISIBILITY
    assert claim["evidence_level"] == "observed"
    assert claim["evidence_state"] == "current"
    assert claim["provenance"]["source"] == "vm_observation"
    assert claim["scope"]["identity_state"] == "confirmed"

    # Immutable YAML bytes & binding_sha256 are strictly unchanged!
    assert sym_path.read_bytes() == raw_before
    binding_after = load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    assert binding_after.binding_sha256 == binding_before.binding_sha256

    claims_doc = load_symbol_claims(profile)
    validate_or_raise("knowledge-claims.v1.json", claims_doc)

    # Exact 5-tuple match -> reusable
    assert has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )

    # Any single mismatch across the 5 keys MUST NOT reuse:
    # 1) Cross-ax_backend
    assert not has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="guest_axruntime",
    )
    # 2) Cross-binding_sha256
    assert not has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=SHA_OTHER,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )
    # 3) Cross-screen_id
    assert not has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S02_preferences",
        ax_backend="frida_inproc",
    )
    # 4) Cross-artifact_content_digest (same version, different build)
    assert not has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V2,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )
    # 5) Cross-symbol_id
    assert not has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.search",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )

    # Spec §3.2 shape using scope.artifact_sha256 (without artifact_content_digest) also works;
    # static_hint claim MUST NOT count as reusable unique_visibility.
    spec_claims = {
        "schema_version": "v1",
        "app": "food_ordering",
        "claims": [
            {
                "id": "clm-static",
                "status": "active",
                "kind": CLAIM_KIND_UNIQUE_VISIBILITY,
                "statement": "static hint only",
                "evidence_level": "static_hint",
                "evidence_state": "current",
                "provenance": {"source": "l2_build_manifest"},
                "scope": {
                    "symbol_id": "ctrl.home.preferences",
                    "artifact_sha256": SHA_V2,
                    "binding_sha256": binding_before.binding_sha256,
                    "screen_id": "S01_home",
                    "identity_state": "confirmed",
                    "ax_backend": "frida_inproc",
                },
                "evidence_refs": [],
            },
            {
                "id": "clm-spec-3-2",
                "status": "active",
                "kind": CLAIM_KIND_UNIQUE_VISIBILITY,
                "statement": "spec 3.2 shape",
                "evidence_level": "observed",
                "evidence_state": "current",
                "provenance": {"source": "vm_observation"},
                "scope": {
                    "symbol_id": "ctrl.home.preferences",
                    "artifact_sha256": SHA_V1,
                    "binding_sha256": binding_before.binding_sha256,
                    "screen_id": "S01_home",
                    "identity_state": "confirmed",
                    "ax_backend": "frida_inproc",
                },
                "evidence_refs": ["runs/run_01/shots/R3_01.ax.json"],
            },
        ],
    }
    save_symbol_claims(profile, spec_claims)
    reloaded = load_symbol_claims(profile)
    assert not has_reusable_unique_visibility(
        reloaded,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V2,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )
    assert has_reusable_unique_visibility(
        reloaded,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_before.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )


# ── 4. ax_backend Plumbing & guest_axruntime Fail-Closed ──────────────────────


def test_ax_session_and_ax_target_backend_plumbing(tmp_path: Path) -> None:
    session = AxSession("com.example.app")
    assert session.ax_backend == "guest_axruntime"
    assert session.host is None

    frida_session = AxSession("com.example.app", ax_backend="frida_inproc")
    assert frida_session.ax_backend == "frida_inproc"
    assert frida_session.host is not None

    guest_session = AxSession("com.example.app", ax_backend="guest_axruntime")
    assert guest_session.ax_backend == "guest_axruntime"
    assert guest_session.host is None

    # Passing borrowed_lifetime without explicit ax_backend selects frida_inproc;
    # combining borrowed_lifetime with guest_axruntime fails closed.
    fake_lifetime = SimpleNamespace(host="127.0.0.1:27042", closed=False, closing=False)
    borrowed_session = AxSession("com.example.app", borrowed_lifetime=fake_lifetime)  # type: ignore[arg-type]
    assert borrowed_session.ax_backend == "frida_inproc"
    assert borrowed_session.host == "127.0.0.1:27042"
    with pytest.raises(ValueError, match="borrowed_lifetime"):
        AxSession(
            "com.example.app",
            borrowed_lifetime=fake_lifetime,  # type: ignore[arg-type]
            ax_backend="guest_axruntime",
        )

    with pytest.raises(UnsupportedAxBackendError) as exc_info:
        AxSession("com.example.app", ax_backend="unknown_backend")  # type: ignore[arg-type]
    assert isinstance(exc_info.value, AxUnavailable)
    assert exc_info.value.code == AX_BACKEND_UNSUPPORTED_CODE
    assert exc_info.value.ax_backend == "unknown_backend"

    # parse_dump & load_dump preserve ax_backend and stay backward-compatible
    raw_dump = {
        "schema": "autophone.ax/1",
        "app": "com.example.app",
        "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
        "nodes": [],
    }
    assert parse_dump(raw_dump).ax_backend is None
    assert parse_dump(raw_dump, ax_backend="frida_inproc").ax_backend == "frida_inproc"

    tagged_path = tmp_path / "dump.ax.json"
    tagged_path.write_text(
        json.dumps({**raw_dump, "ax_backend": "guest_axruntime"}),
        encoding="utf-8",
    )
    assert load_dump(tagged_path).ax_backend == "guest_axruntime"

    with pytest.raises(AxError, match="ax_backend mismatch"):
        parse_dump({**raw_dump, "ax_backend": "guest_axruntime"}, ax_backend="frida_inproc")

    # AxSession.refresh & snapshot stamp ax_backend="frida_inproc"
    out_file = tmp_path / "pulled.json"
    recorded_req: dict[str, str] = {}

    class _FakeExports:
        def prepare(self, req_id: str) -> int:
            recorded_req["id"] = req_id
            return 0

        def post(self, _notif: str) -> int:
            return 0

    def _fake_request(cmd: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], None]:
        assert cmd["t"] == "file_get"
        payload = {
            "schema": "autophone.ax/1",
            "evidence_protocol": 1,
            "request_id": recorded_req["id"],
            "captured_at_ms": 1732622400001 + len(recorded_req),
            "app": "com.example.app",
            "complete": True,
            "truncated": False,
            "short_version": "1.0.0",
            "bundle_version": "1",
            "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
            "nodes": [],
        }
        Path(cmd["save"]).write_text(json.dumps(payload), encoding="utf-8")
        return {"ok": True}, None

    live = AxSession(
        "com.example.app",
        out=str(out_file),
        settle_s=0.0,
        request_fn=_fake_request,
        ax_backend="frida_inproc",
    )
    live._script = SimpleNamespace(exports_sync=_FakeExports())
    refreshed = live.refresh(request_id="req-1")
    assert refreshed.ax_backend == "frida_inproc"

    # Increment timestamp for next pull in snapshot()
    live._last_captured_at_ms = 0
    snap = live.snapshot()
    assert snap["ax_backend"] == "frida_inproc"


def test_guest_axruntime_supported_and_unknown_backend_fails_closed(tmp_path: Path) -> None:
    frida_profile = _make_profile(tmp_path, app="app_frida", ax_enabled=True, ax_backend="frida_inproc")
    guest_profile = _make_profile(tmp_path, app="app_guest", ax_enabled=True, ax_backend="guest_axruntime")
    guest_disabled_profile = _make_profile(
        tmp_path, app="app_guest_off", ax_enabled=False, ax_backend="guest_axruntime"
    )
    unknown_profile = SimpleNamespace(
        app="app_unknown",
        bundle_id="com.example.unknown",
        ax_backend="unknown_backend",
        raw={"capabilities": {"ax": {"enabled": True, "backend": "unknown_backend"}}},
    )
    unknown_disabled_profile = SimpleNamespace(
        app="app_unknown_off",
        bundle_id="com.example.unknown.off",
        ax_backend="unknown_backend",
        raw={"capabilities": {"ax": {"enabled": False, "backend": "unknown_backend"}}},
    )

    default_profile = _make_profile(tmp_path, app="app_default", ax_enabled=True, ax_backend=None)
    assert ensure_ax_backend_supported(frida_profile) == "frida_inproc"
    assert ensure_ax_backend_supported("frida_inproc") == "frida_inproc"
    assert ensure_ax_backend_supported(None) == "guest_axruntime"
    assert ensure_ax_backend_supported(default_profile) == "guest_axruntime"
    assert ensure_ax_backend_supported(guest_profile) == "guest_axruntime"
    assert ensure_ax_backend_supported("guest_axruntime") == "guest_axruntime"

    for bad in (unknown_profile, unknown_disabled_profile, "unknown_backend", "bad_backend"):
        with pytest.raises(UnsupportedAxBackendError) as exc_info:
            ensure_ax_backend_supported(bad)
        assert exc_info.value.code == AX_BACKEND_UNSUPPORTED_CODE

    # build_ax_session & build_run_ax_session default to guest_axruntime, preserve explicit frida_inproc, and fail closed on unknown
    built_default = build_ax_session(default_profile)
    assert built_default is not None and built_default.ax_backend == "guest_axruntime"
    built = build_ax_session(frida_profile)
    assert built is not None and built.ax_backend == "frida_inproc"
    built_guest = build_ax_session(guest_profile)
    assert built_guest is not None and built_guest.ax_backend == "guest_axruntime"
    assert build_ax_session(guest_disabled_profile) is None
    run_dir = tmp_path / "run_01"
    run_dir.mkdir()
    built_default_run = build_run_ax_session(default_profile, run_dir)
    assert built_default_run is not None and built_default_run.ax_backend == "guest_axruntime"
    built_run = build_run_ax_session(frida_profile, run_dir)
    assert built_run is not None and built_run.ax_backend == "frida_inproc"
    built_guest_run = build_run_ax_session(guest_profile, run_dir)
    assert built_guest_run is not None and built_guest_run.ax_backend == "guest_axruntime"

    with pytest.raises(UnsupportedAxBackendError):
        build_ax_session(unknown_profile)
    with pytest.raises(UnsupportedAxBackendError):
        build_run_ax_session(unknown_profile, run_dir)
    with pytest.raises(UnsupportedAxBackendError):
        build_run_ax_session(unknown_disabled_profile, run_dir)

    # CRITICAL: ax_scope & lazy_ax_scope MUST NOT swallow UnsupportedAxBackendError!
    class _UnknownStub:
        ax_backend = "unknown_backend"

        def open(self) -> None:
            raise AssertionError("open() should not be reached before backend guard")

        def close(self) -> None:
            pass

    class _OpenRaisesUnsupportedStub:
        ax_backend = "frida_inproc"

        def open(self) -> None:
            raise UnsupportedAxBackendError("unknown_backend")

        def close(self) -> None:
            pass

    class _OpenRaisesUnavailableStub:
        ax_backend = "frida_inproc"

        def open(self) -> None:
            raise AxUnavailable("frida tunnel down")

        def close(self) -> None:
            pass

    with pytest.raises(UnsupportedAxBackendError):
        with ax_scope(_UnknownStub()):
            pass

    with pytest.raises(UnsupportedAxBackendError):
        with ax_scope(_OpenRaisesUnsupportedStub()):
            pass

    # Regular AxUnavailable still fails open in ax_scope (existing behavior preserved!)
    with ax_scope(_OpenRaisesUnavailableStub()) as yielded:
        assert yielded is None

    with pytest.raises(UnsupportedAxBackendError):
        LazyAxSession(_UnknownStub())

    with pytest.raises(UnsupportedAxBackendError):
        with lazy_ax_scope(_UnknownStub()):
            pass

    # LazyAxSession preserves UnsupportedAxBackendError across repeated calls
    lazy_bad = LazyAxSession(_OpenRaisesUnsupportedStub())
    with pytest.raises(UnsupportedAxBackendError):
        lazy_bad.open()
    with pytest.raises(UnsupportedAxBackendError):
        lazy_bad.open()

    # wait_for_spawn_ready MUST NOT swallow UnsupportedAxBackendError either
    ready_profile = SimpleNamespace(
        app="app_ready_unknown",
        bundle_id="com.example.ready.unknown",
        ax_backend="unknown_backend",
        raw={
            "capabilities": {
                "ax": {"enabled": True, "backend": "unknown_backend"},
                "api_capture": {"enabled": True, "ready_markers": ["Home"]},
            }
        },
    )
    shot_dir = run_dir / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(UnsupportedAxBackendError):
        wait_for_spawn_ready(
            shot_dir,
            ready_profile,
            timeout_s=0.0,
            request_fn=lambda _req, **_kw: ({"image": "aGVsbG8="}, None),
            ocr_runner=lambda _path: {"boxes": []},
            sleep_fn=lambda _s: None,
        )


# ── 5. ax_checkpoint v2 + build-identity.v2.json + ax_backend ─────────────────


def test_ax_checkpoint_v2_stamps_ax_backend_with_build_identity_v2(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_cp"
    shots = run_dir / "shots"
    shots.mkdir(parents=True)
    shot_file = shots / "cp_01.jpg"
    shot_file.write_bytes(b"\xff\xd8\xff\xd9")

    bi_v2 = _build_identity_v2(identity_state="confirmed", with_candidate=True)
    (run_dir / "build-identity.json").write_text(json.dumps(bi_v2), encoding="utf-8")

    class _Provider:
        ax_backend = "frida_inproc"

        def snapshot(self) -> dict[str, Any]:
            return {
                "schema": "autophone.ax/1",
                "evidence_protocol": 1,
                "request_id": "req-cp-1",
                "captured_at_ms": 1732622400000,
                "app": "com.autophone.foodorderingdemo.v2",
                "complete": True,
                "truncated": False,
                "short_version": "1.0.0",
                "bundle_version": "1",
                "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
                "nodes": [
                    {
                        "path": "0/1",
                        "label": "Preferences",
                        "role": "button",
                        "traits": ["button"],
                        "enabled": True,
                        "value": None,
                        "identifier": "home.ai_prompt.preferences",
                        "frame": [20.0, 200.0, 120.0, 44.0],
                    }
                ],
            }

    identity = {
        "run_id": "run_20261126_01",
        "app": "food_ordering",
        "build": "1.0.0-1",
        "bundle_id": "com.autophone.foodorderingdemo.v2",
        "operation": {"id": "OP1", "name": "open_preferences", "revision": 1},
    }
    checkpoint = {
        "expected": "S01_home",
        "matched": "S01_home",
        "shot": "shots/cp_01.jpg",
        "attempt": 1,
    }
    build_evidence = {
        "package": {
            "registry_build_ref": {
                "app": "food_ordering",
                "build_id": "bld-14282f675363",
                "source_revision": {"kind": "unknown", "state": "unknown"},
            },
            "device_build_observation": {
                "bundle_id": "com.autophone.foodorderingdemo.v2",
                "short_version": "1.0.0",
                "build_version": "1",
            },
            "build_identity_match": "match",
            "comparison_basis": "candidate_build_ref",
            "comparison_granularity": "info_plist",
            "comparison_build_id": "bld-14282f675363",
        },
        "sources": {
            "run-submission.json": {"path": "run-submission.json", "sha256": SHA_V1},
            "world-identity.json": {"path": "world-identity.json", "sha256": SHA_V2},
        },
    }

    outcome = ax_checkpoint.capture(
        _Provider(),
        run_dir=run_dir,
        identity=identity,
        checkpoint=checkpoint,
        label="after_home",
        build_evidence=build_evidence,
    )
    assert outcome["status"] == "captured"
    assert outcome["ax_backend"] == "frida_inproc"

    saved = json.loads((run_dir / outcome["path"]).read_text(encoding="utf-8"))
    validate_or_raise("operation-ax-checkpoint.v2.json", saved)
    assert saved["schema_version"] == "v2"
    assert saved["ax_backend"] == "frida_inproc"
    assert saved["snapshot"]["ax_backend"] == "frida_inproc"
    assert saved["build_identity"]["schema_version"] == "v2"
    assert saved["build_identity"]["candidate_identity"]["main_jsbundle_sha256"] == SHA_JS_1

    # v1 checkpoint (build_evidence=None) stamps snapshot.ax_backend without violating v1 strict root
    bi_v1 = {
        "schema_version": "v1",
        "run_id": "run_20261126_01",
        "app": "food_ordering",
        "identity_state": "confirmed",
        "identity_source": "combined",
        "subject_build_key": "build-v1:" + SHA_V1,
        "device_observation": {
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
        },
        "match": {"state": "exact"},
        "subject_epochs": [],
        "updated_at": "2026-11-26T12:00:00Z",
    }
    (run_dir / "build-identity.json").write_text(json.dumps(bi_v1), encoding="utf-8")
    outcome_v1 = ax_checkpoint.capture(
        _Provider(),
        run_dir=run_dir,
        identity=identity,
        checkpoint=checkpoint,
        label="after_home_v1",
        build_evidence=None,
    )
    assert outcome_v1["status"] == "captured"
    saved_v1 = json.loads((run_dir / outcome_v1["path"]).read_text(encoding="utf-8"))
    validate_or_raise("operation-ax-checkpoint.v1.json", saved_v1)
    assert "ax_backend" not in saved_v1
    assert saved_v1["snapshot"]["ax_backend"] == "frida_inproc"

    # UnsupportedAxBackendError raised by provider.snapshot() MUST NOT be swallowed
    class _UnsupportedProvider:
        def snapshot(self) -> dict[str, Any]:
            raise UnsupportedAxBackendError("guest_axruntime")

    with pytest.raises(UnsupportedAxBackendError):
        ax_checkpoint.capture(
            _UnsupportedProvider(),
            run_dir=run_dir,
            identity=identity,
            checkpoint=checkpoint,
            label="after_home",
            build_evidence=build_evidence,
        )


# ── 6. run-result.v1.json & RunResultAssembler ────────────────────────────────


def test_run_result_schema_and_assembler_ax_backend_and_binding_sha256() -> None:
    spec = {
        "run_id": "run_20261126_02",
        "type": "regression",
        "app": "food_ordering",
        "device": "rig-a",
        "planes": {
            "ui": {"mode": "required"},
            "function": {"mode": "disabled"},
            "api": {"mode": "disabled"},
            "telemetry": {"mode": "disabled"},
        },
    }
    package = {
        "default_slot": "001",
        "default_version": "1.0.0",
        "default_sha256": SHA_V1,
        "device_version": "1.0.0",
        "profile_build_label": "1.0.0-bld-14282f675363",
    }
    phase2 = phase2_result_from_json(
        {
            "aborted": False,
            "abort_reason": "",
            "ax_backend": "frida_inproc",
            "cases": [
                {
                    "id": "R3",
                    "status": "pass",
                    "detail": "",
                    "exec_via": "recipe",
                    "binding_sha256": SHA_V2,
                    "recipe_identity": {
                        "source": "live",
                        "path": "recipes/r3.yaml",
                        "recipe_sha256": SHA_V1,
                        "binding_sha256": SHA_V2,
                        "case_revision": 1,
                        "reserved_external_id": None,
                    },
                    "negative_control": {
                        "control_id": "nc-1",
                        "declaration_sha256": SHA_ASSETS,
                        "applied": True,
                        "perturbation": {"kind": "skip_step", "step_index": 0},
                        "expect": {"assertion_step_index": 1},
                        "base_recipe_identity": None,
                        "perturbed_steps_digest": SHA_JS_1,
                        "failed_step_index": 1,
                        "fail_kind": "assertion_failed",
                        "binding_sha256": SHA_V2,
                    },
                }
            ],
        }
    )

    assembler = RunResultAssembler()
    result = assembler.assemble(
        spec,
        phase2,
        package=package,
        timestamps={"execute_started_at": "2026-11-26T12:00:00Z"},
        phase_status={"execute": {"status": "ok"}},
    )
    validate_or_raise("run-result.v1.json", result)
    assert result["ax_backend"] == "frida_inproc"
    assert result["planes"]["ui"]["ax_backend"] == "frida_inproc"
    assert result["cases"][0]["planes"]["ui"]["ax_backend"] == "frida_inproc"
    assert result["cases"][0]["binding_sha256"] == SHA_V2
    assert result["cases"][0]["recipe_identity"]["binding_sha256"] == SHA_V2
    assert result["cases"][0]["negative_control"]["binding_sha256"] == SHA_V2

    # refresh_telemetry preserves ax_backend and binding_sha256
    refreshed = assembler.refresh_telemetry(spec, phase2, result, None)
    validate_or_raise("run-result.v1.json", refreshed)
    assert refreshed["ax_backend"] == "frida_inproc"
    assert refreshed["planes"]["ui"]["ax_backend"] == "frida_inproc"
    assert refreshed["cases"][0]["binding_sha256"] == SHA_V2

    # Null binding_sha256 & null ax_backend are schema-valid
    nullable = json.loads(json.dumps(result))
    nullable["ax_backend"] = None
    nullable["planes"]["ui"]["ax_backend"] = None
    nullable["cases"][0]["binding_sha256"] = None
    nullable["cases"][0]["recipe_identity"]["binding_sha256"] = None
    nullable["cases"][0]["negative_control"]["binding_sha256"] = None
    validate_or_raise("run-result.v1.json", nullable)

    # Invalid ax_backend and malformed binding_sha256 are rejected by run-result.v1.json
    bad_backend = json.loads(json.dumps(result))
    bad_backend["ax_backend"] = "xcuitest"
    with pytest.raises(ContractError):
        validate_or_raise("run-result.v1.json", bad_backend)

    bad_binding = json.loads(json.dumps(result))
    bad_binding["cases"][0]["binding_sha256"] = "not-a-sha256"
    with pytest.raises(ContractError):
        validate_or_raise("run-result.v1.json", bad_binding)
