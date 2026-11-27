"""Self-contained synthetic unit & CLI tests for Milestone M6 symbol toolchain."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import plistlib
from typing import Any
import zipfile

import pytest
import yaml

from autophone import cli as autophone_cli
from autophone.assets.app_symbols import (
    APP_SYMBOLS_SCHEMA_FILE,
    CLAIM_KIND_EFFECT_SET,
    CLAIM_KIND_LOCATOR_BINDING,
    KNOWLEDGE_CLAIMS_SCHEMA_FILE,
    SymbolBindingMismatchError,
    canonical_rn_candidate_content_digest,
    load_symbol_binding,
    load_symbol_binding_file,
    load_symbol_claims,
)
from autophone.assets.apps import AppProfile
from autophone.assets.symbol_tools import (
    DEFAULT_L1_SCREEN_ID,
    HERMES_BYTECODE_MAGIC,
    PROVENANCE_L1_EXTRACTOR,
    PROVENANCE_L2_MANIFEST,
    SymbolToolError,
    compute_dual_denominator_metrics,
    extract_l1_rn_symbols,
    extract_rn_identifiers_from_bundle,
    import_l2_manifest,
)
from autophone.contracts import validate_or_raise

SHA_V1 = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
SHA_V2 = "25393f786474b0e85472d0c6e35edef572759ef3b24d0abb5599e1bbe5079e69"
SHA_ASSETS = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
SHA_RIG = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"


# ── Synthetic Fixture Helpers ─────────────────────────────────────────────────


def _make_profile(
    root: Path,
    *,
    app: str = "food_ordering",
    bundle_id: str = "com.autophone.foodorderingdemo.v2",
) -> AppProfile:
    raw: dict[str, Any] = {
        "schema_version": "v1",
        "app": app,
        "bundle_id": bundle_id,
        "display_name": "Food Ordering",
    }
    app_dir = root / "apps" / app
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "profile.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return AppProfile(raw=raw, root=root, state_root=root)


def _write_v2_package_manifest(
    root: Path,
    *,
    app: str = "food_ordering",
    bundle_id: str = "com.autophone.foodorderingdemo.v2",
    build_id: str = "bld-14282f675363",
    sha256: str = SHA_V1,
    short_version: str = "1.0.0",
    build_version: str = "1",
) -> None:
    manifest = {
        "schema_version": "v2",
        "app": app,
        "bundle_id": bundle_id,
        "default_slot": "001",
        "version_slots": [
            {
                "slot": "001",
                "short_version": short_version,
                "candidate_build_id": build_id,
                "created_at": "2026-11-26T10:00:00Z",
            }
        ],
        "builds": [
            {
                "build_id": build_id,
                "slot": "001",
                "label": "v1",
                "bundle_id": bundle_id,
                "build_version": build_version,
                "source_revision": {"kind": "unknown", "state": "unknown"},
                "artifact": {
                    "file": f"{build_id}.ipa",
                    "sha256": sha256,
                    "size": 1024,
                },
                "artifact_availability": "present",
                "release_like": True,
                "release_evidence": "embedded_js_bundle:Payload/Demo.app/main.jsbundle",
                "legacy_slot": None,
                "added_at": "2026-11-26T10:00:00Z",
            }
        ],
    }
    path = root / "packages" / app / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _make_synthetic_ipa(
    path: Path,
    *,
    app_bundle_name: str = "FoodDemo.app",
    bundle_id: str = "com.autophone.foodorderingdemo.v2",
    short_version: str = "1.0.0",
    build_version: str = "1",
    jsbundle_bytes: bytes | None = None,
    plist_format: plistlib.PlistFormat = plistlib.FMT_BINARY,
    include_info_plist: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if include_info_plist:
            plist_bytes = plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleShortVersionString": short_version,
                    "CFBundleVersion": build_version,
                    "CFBundleName": "FoodDemo",
                },
                fmt=plist_format,
            )
            zf.writestr(f"Payload/{app_bundle_name}/Info.plist", plist_bytes)
        if jsbundle_bytes is not None:
            zf.writestr(f"Payload/{app_bundle_name}/main.jsbundle", jsbundle_bytes)
        # Nested privacy bundle Info.plist must be ignored when locating top-level app Info.plist.
        zf.writestr(
            f"Payload/{app_bundle_name}/SomeSDK.bundle/Info.plist",
            plistlib.dumps({"CFBundleIdentifier": "org.sdk.nested"}, fmt=plistlib.FMT_XML),
        )
    path.write_bytes(buf.getvalue())
    return path


# ── 1. L2 Thin Manifest Importer ──────────────────────────────────────────────


def test_import_l2_manifest_with_package_store_build_id_and_immutable_hash(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    _write_v2_package_manifest(tmp_path)

    manifest_path = tmp_path / "manifests" / "l2-symbols.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_doc = {
        "app": "food_ordering",
        "controls": [
            {
                "symbol_id": "ctrl.home.preferences",
                "kind": "button",
                "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": "home.ai_prompt.preferences",
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {"verbs": [], "completeness": "exhaustive"},
            },
            {
                "symbol_id": "ctrl.checkout.submit",
                "kind": "button",
                "scope": {"screen_id": "S02_checkout"},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": "checkout.submit",
                    "role": "button",
                },
                "effects": {"verbs": ["place_order", "pay"], "completeness": "exhaustive"},
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest_doc), encoding="utf-8")

    summary = import_l2_manifest(
        profile,
        manifest_path,
        build_id="bld-14282f675363",
        package_root=tmp_path,
    )

    symbol_path = Path(summary["symbol_path"])
    assert symbol_path == tmp_path / "apps" / "food_ordering" / "symbols" / "artifacts" / f"{SHA_V1[:12]}.yaml"
    assert symbol_path.is_file()
    assert summary["artifact_content_digest"] == SHA_V1
    assert summary["artifact_kind"] == "ipa_build"
    assert summary["build_id"] == "bld-14282f675363"
    assert summary["control_count"] == 2
    assert summary["claims_written"] == 4  # 2 locator_binding + 2 effect_set
    assert summary["claims_skipped"] == 0

    # Verify raw file byte hash equals binding_sha256 (untouched by claims write).
    raw_bytes = symbol_path.read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == summary["binding_sha256"]
    parsed_yaml = yaml.safe_load(raw_bytes.decode("utf-8"))
    validate_or_raise(APP_SYMBOLS_SCHEMA_FILE, parsed_yaml)
    assert "relation_claims" not in parsed_yaml
    assert "target_fingerprint" not in parsed_yaml["artifact_identity"]

    # Loader routes the artifact cleanly and reproduces identical binding_sha256.
    loaded = load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    assert loaded.binding_sha256 == summary["binding_sha256"]
    assert loaded.require_control("ctrl.home.preferences").locator.identifier == "home.ai_prompt.preferences"
    assert loaded.require_control("ctrl.checkout.submit").effects.verbs == frozenset({"place_order", "pay"})

    # Validate external claims against knowledge-claims.v1.json.
    claims_doc = load_symbol_claims(profile)
    validate_or_raise(KNOWLEDGE_CLAIMS_SCHEMA_FILE, claims_doc)
    assert len(claims_doc["claims"]) == 4
    kinds = {c["kind"] for c in claims_doc["claims"]}
    assert kinds == {CLAIM_KIND_LOCATOR_BINDING, CLAIM_KIND_EFFECT_SET}
    for claim in claims_doc["claims"]:
        assert claim["provenance"]["source"] == PROVENANCE_L2_MANIFEST
        assert claim["evidence_level"] == "static_hint"
        assert claim["evidence_state"] == "current"
        assert claim["scope"]["binding_sha256"] == summary["binding_sha256"]
        assert claim["scope"]["artifact_content_digest"] == SHA_V1
        assert claim["scope"]["artifact_sha256"] == SHA_V1

    # Idempotent re-import preserves binding_sha256 and skips duplicate claims.
    rerun = import_l2_manifest(
        profile,
        manifest_path,
        build_id="bld-14282f675363",
        package_root=tmp_path,
    )
    assert rerun["binding_sha256"] == summary["binding_sha256"]
    assert rerun["claims_written"] == 0
    assert rerun["claims_skipped"] == 4
    assert symbol_path.read_bytes() == raw_bytes


def test_import_l2_manifest_rn_candidate_strips_target_fingerprint_and_omitted_effects(
    tmp_path: Path,
) -> None:
    manifest_yaml = tmp_path / "rn-manifest.yaml"
    js_sha = hashlib.sha256(b"rn-js-payload").hexdigest()
    manifest_yaml.write_text(
        yaml.safe_dump(
            {
                "artifact_identity": {
                    "kind": "rn_candidate",
                    "target_fingerprint": f"sha256:{SHA_RIG}",
                    "native_runtime_id": SHA_V2,
                    "update_source": "ota",
                    "bundle_id": "com.autophone.foodorderingdemo.v2",
                    "short_version": "1.0.0",
                    "build_version": "1",
                    "base_ipa_sha256": SHA_V1,
                    "main_jsbundle_sha256": js_sha,
                    "assets_tree_sha256": SHA_ASSETS,
                },
                "controls": {
                    "ctrl.home.preferences": {
                        "kind": "button",
                        "scope": {"screen_id": "S01_home"},
                        "locator": {
                            "identifier": "home.ai_prompt.preferences",
                            "role": None,
                        },
                        # effects intentionally omitted -> must default to unknown and skip effect_set claim
                    },
                    "ctrl.home.cart": {
                        "kind": "button",
                        "scope": {"screen_id": "S01_home"},
                        "locator": {
                            "identifier": "home.header.cart",
                            "role": "button",
                        },
                        "effects": {"verbs": [], "completeness": "exhaustive"},
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    expected_digest = canonical_rn_candidate_content_digest(
        base_ipa_sha256=SHA_V1,
        main_jsbundle_sha256=js_sha,
        assets_tree_sha256=SHA_ASSETS,
    )
    summary = import_l2_manifest(
        "food_ordering",
        manifest_yaml,
        output_root=tmp_path,
    )
    assert summary["artifact_kind"] == "rn_candidate"
    assert summary["artifact_content_digest"] == expected_digest
    assert summary["build_id"] is None
    assert summary["control_count"] == 2
    assert summary["claims_written"] == 3  # 2 locator_binding + 1 effect_set (only ctrl.home.cart)

    loaded = load_symbol_binding_file(summary["symbol_path"])
    assert "target_fingerprint" not in loaded.artifact_identity
    assert loaded.require_control("ctrl.home.preferences").effects.completeness == "unknown"
    assert loaded.require_control("ctrl.home.preferences").effects.verbs == frozenset()
    assert loaded.require_control("ctrl.home.cart").effects.completeness == "exhaustive"

    claims_doc = load_symbol_claims(tmp_path / "apps" / "food_ordering", app="food_ordering")
    validate_or_raise(KNOWLEDGE_CLAIMS_SCHEMA_FILE, claims_doc)
    effect_claims = [c for c in claims_doc["claims"] if c["kind"] == CLAIM_KIND_EFFECT_SET]
    assert [c["scope"]["symbol_id"] for c in effect_claims] == ["ctrl.home.cart"]
    for claim in claims_doc["claims"]:
        assert "target_fingerprint" not in claim["scope"]
        assert "artifact_sha256" not in claim["scope"]
        assert claim["scope"]["artifact_content_digest"] == expected_digest


def test_import_l2_manifest_fail_closed_guards(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    _write_v2_package_manifest(tmp_path)

    # Missing both build_id and artifact_identity.
    with pytest.raises(SymbolToolError, match="requires either --build-id"):
        import_l2_manifest(profile, {"controls": []}, output_root=tmp_path)

    # App mismatch.
    with pytest.raises(SymbolBindingMismatchError, match="does not match"):
        import_l2_manifest(
            profile,
            {"app": "other_app", "controls": []},
            build_id="bld-14282f675363",
            package_root=tmp_path,
        )

    # Conflicting artifact_sha256 vs Package Store.
    with pytest.raises(SymbolBindingMismatchError, match="contradicts Package Store"):
        import_l2_manifest(
            profile,
            {
                "artifact_identity": {
                    "kind": "ipa_build",
                    "bundle_id": "com.autophone.foodorderingdemo.v2",
                    "short_version": "1.0.0",
                    "build_version": "1",
                    "build_id": "bld-14282f675363",
                    "artifact_sha256": SHA_V2,
                },
                "controls": [],
            },
            build_id="bld-14282f675363",
            package_root=tmp_path,
        )

    # Overwrite protection when content changes for the same digest.
    first_manifest = {
        "controls": [
            {
                "symbol_id": "ctrl.home.preferences",
                "kind": "button",
                "scope": {"screen_id": "S01_home"},
                "locator": {"identifier": "home.ai_prompt.preferences"},
            }
        ]
    }
    first = import_l2_manifest(
        profile, first_manifest, build_id="bld-14282f675363", package_root=tmp_path
    )
    second_manifest = {
        "controls": [
            {
                "symbol_id": "ctrl.home.preferences",
                "kind": "button",
                "scope": {"screen_id": "S01_home"},
                "locator": {"identifier": "home.ai_prompt.preferences.v2"},
            }
        ]
    }
    with pytest.raises(SymbolToolError, match="already exists with different content"):
        import_l2_manifest(
            profile, second_manifest, build_id="bld-14282f675363", package_root=tmp_path
        )
    replaced = import_l2_manifest(
        profile,
        second_manifest,
        build_id="bld-14282f675363",
        package_root=tmp_path,
        overwrite=True,
    )
    assert replaced["binding_sha256"] != first["binding_sha256"]


# ── 2. L1 Minimal RN main.jsbundle Blind Extractor ────────────────────────────


def test_extract_l1_rn_plain_js_ipa_and_noise_filtering(tmp_path: Path) -> None:
    js_bundle = (
        b'var __BUNDLE_START_TIME__=Date.now();'
        b'var props=["nativeID","testID","onLayout"];'
        b'if(x.testID&&x.testID.startsWith("debugClassName")){console.log(x.testID);}'
        b'(0,p.jsx)(c.ObservedPressable,{accessibilityLabel:"\\u8c03\\u504f\\u597d",'
        b'accessibilityRole:"button",onPress:function(){s.Keyboard.dismiss(),r.router.push("/preferences")},'
        b'style:R.promptPreferences,testID:"home.ai_prompt.preferences"});'
        b'(0,p.jsx)(s.View,{testID:"home.header",children:(0,p.jsx)(s.Pressable,{accessibilityRole:"button"})});'
        b'(0,p.jsx)(s.TextInput,{testID:\'search.query_input\'});'
        b'(0,p.jsx)(s.Pressable,{"accessibilityIdentifier":"checkout.pay_now"});'
        b'<Button testID="settings.reset_btn" />'
        # Duplicate should deduplicate cleanly:
        b'(0,p.jsx)(s.View,{testID:"home.ai_prompt.preferences"});'
        # Dynamic & noise fragments that MUST be filtered out:
        b'{testID:t,other:1};'
        b'{testID:!0};'
        b'{testID:this.props.testID};'
        b'{testID:"item-" + idx};'
        b'{testID:"row_".concat(idx)};'
        b'{testID:"tpl_${id}"};'
        b'{testID:"mustache-{{id}}"};'
        b'{testID:"   "};'
        b'{testID:"x"};'
        b'{testID:"https://example.com/path"};'
        b'{testID:"has space inside"};'
    )
    ipa = _make_synthetic_ipa(tmp_path / "FoodDemo.ipa", jsbundle_bytes=js_bundle)
    expected_ipa_sha = hashlib.sha256(ipa.read_bytes()).hexdigest()

    summary = extract_l1_rn_symbols(
        "food_ordering",
        ipa_path=ipa,
        output_root=tmp_path,
    )

    assert summary["source"] == PROVENANCE_L1_EXTRACTOR
    assert summary["bundle_format"] == "plain_js"
    assert summary["artifact_content_digest"] == expected_ipa_sha
    assert summary["build_id"] == f"bld-{expected_ipa_sha[:12]}"
    assert summary["bundle_id"] == "com.autophone.foodorderingdemo.v2"
    assert summary["short_version"] == "1.0.0"
    assert summary["build_version"] == "1"
    assert summary["extracted_identifiers"] == [
        "checkout.pay_now",
        "home.ai_prompt.preferences",
        "home.header",
        "search.query_input",
        "settings.reset_btn",
    ]
    assert summary["control_count"] == 5
    assert summary["claims_written"] == 5

    symbol_path = Path(summary["symbol_path"])
    assert symbol_path.name == f"{expected_ipa_sha[:12]}.yaml"
    raw_bytes = symbol_path.read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == summary["binding_sha256"]

    loaded = load_symbol_binding_file(symbol_path, expected_app="food_ordering")
    pref = loaded.require_control("ctrl.rn.home.ai_prompt.preferences")
    assert pref.kind == "button"
    assert pref.screen_id == DEFAULT_L1_SCREEN_ID
    assert pref.locator.kind == "ax_identifier"
    assert pref.locator.identifier == "home.ai_prompt.preferences"
    assert pref.locator.role is None
    assert pref.locator.enabled_only is True
    assert pref.locator.require_unique is True
    assert loaded.require_control("ctrl.rn.search.query_input").kind == "input"
    assert loaded.require_control("ctrl.rn.home.header").kind == "control"

    # EVERY L1 control MUST have effects.completeness == "unknown" and verbs == frozenset().
    for ctrl in loaded.controls.values():
        assert ctrl.effects.completeness == "unknown"
        assert ctrl.effects.verbs == frozenset()

    # Validate external l1_ipa_extractor claims against knowledge-claims.v1.json.
    claims_doc = load_symbol_claims(tmp_path / "apps" / "food_ordering", app="food_ordering")
    validate_or_raise(KNOWLEDGE_CLAIMS_SCHEMA_FILE, claims_doc)
    assert len(claims_doc["claims"]) == 5
    for claim in claims_doc["claims"]:
        assert claim["status"] == "draft"
        assert claim["kind"] == CLAIM_KIND_LOCATOR_BINDING
        assert claim["provenance"]["source"] == PROVENANCE_L1_EXTRACTOR
        assert claim["evidence_level"] == "static_hint"
        assert claim["evidence_state"] == "current"
        assert claim["scope"]["binding_sha256"] == summary["binding_sha256"]
        assert claim["scope"]["artifact_content_digest"] == expected_ipa_sha


def test_extract_l1_rn_hermes_bytecode_patterns(tmp_path: Path) -> None:
    # Construct a synthetic Hermes bytecode blob with magic header + null-delimited string pool.
    hermes_payload = (
        HERMES_BYTECODE_MAGIC
        + b"\x60\x00\x00\x00"  # version 96
        + b"\x00" * 32
        + b"\x00testID\x00home.ai_prompt.preferences\x00"
        + b"accessibilityIdentifier\x00checkout.confirm_btn\x00"
        + b"cart.empty.primary_action\x00"
        # Noise that must be rejected:
        + b"index.bundle.js\x00"
        + b"com.apple.uikit\x00"
        + b"react.component\x00"
        + b"Object.prototype\x00"
        + b"item.${id}\x00"
        + b"https://example.com\x00"
        + b"styles.container\x00"
    )
    direct = extract_rn_identifiers_from_bundle(hermes_payload)
    assert direct.bundle_format == "hermes_bytecode"
    assert direct.identifiers == [
        "cart.empty.primary_action",
        "checkout.confirm_btn",
        "home.ai_prompt.preferences",
    ]

    ipa = _make_synthetic_ipa(
        tmp_path / "FoodHermes.ipa",
        jsbundle_bytes=hermes_payload,
        plist_format=plistlib.FMT_XML,
    )
    summary = extract_l1_rn_symbols(
        "food_ordering",
        ipa_path=ipa,
        default_screen_id="S01_home",
        output_root=tmp_path,
    )
    assert summary["bundle_format"] == "hermes_bytecode"
    assert "home.ai_prompt.preferences" in summary["extracted_identifiers"]
    loaded = load_symbol_binding_file(summary["symbol_path"])
    assert loaded.require_control("ctrl.rn.home.ai_prompt.preferences").screen_id == "S01_home"
    assert loaded.require_control("ctrl.rn.home.ai_prompt.preferences").effects.completeness == "unknown"


def test_extract_l1_rn_standalone_bundle_with_rn_candidate_identity(tmp_path: Path) -> None:
    bundle_bytes = b'({testID:"home.ai_prompt.preferences",accessibilityRole:"button"})'
    bundle_path = tmp_path / "main.jsbundle"
    bundle_path.write_bytes(bundle_bytes)
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()

    rn_identity = {
        "kind": "rn_candidate",
        "target_fingerprint": f"sha256:{SHA_RIG}",
        "bundle_id": "com.autophone.foodorderingdemo.v2",
        "short_version": "1.0.0",
        "build_version": "2",
        "base_ipa_sha256": SHA_V1,
        "main_jsbundle_sha256": bundle_sha,
        "assets_tree_sha256": SHA_ASSETS,
    }
    summary = extract_l1_rn_symbols(
        "food_ordering",
        bundle_path=bundle_path,
        artifact_identity=rn_identity,
        output_root=tmp_path,
    )
    expected_digest = canonical_rn_candidate_content_digest(
        base_ipa_sha256=SHA_V1,
        main_jsbundle_sha256=bundle_sha,
        assets_tree_sha256=SHA_ASSETS,
    )
    assert summary["artifact_kind"] == "rn_candidate"
    assert summary["artifact_content_digest"] == expected_digest
    loaded = load_symbol_binding_file(summary["symbol_path"])
    assert "target_fingerprint" not in loaded.artifact_identity

    # Mismatched bundle sha256 fails closed.
    bad_identity = {**rn_identity, "main_jsbundle_sha256": SHA_V2}
    with pytest.raises(SymbolBindingMismatchError, match="does not match"):
        extract_l1_rn_symbols(
            "food_ordering",
            bundle_path=bundle_path,
            artifact_identity=bad_identity,
            output_root=tmp_path,
        )


def test_extract_l1_rn_fails_cleanly_on_missing_jsbundle_or_bad_ipa(tmp_path: Path) -> None:
    ipa_no_js = _make_synthetic_ipa(tmp_path / "NativeOnly.ipa", jsbundle_bytes=None)
    with pytest.raises(SymbolToolError, match="main.jsbundle not found"):
        extract_l1_rn_symbols("food_ordering", ipa_path=ipa_no_js, output_root=tmp_path)

    ipa_no_plist = _make_synthetic_ipa(
        tmp_path / "NoPlist.ipa",
        jsbundle_bytes=b'({testID:"home.root"})',
        include_info_plist=False,
    )
    with pytest.raises(SymbolToolError, match="Info.plist"):
        extract_l1_rn_symbols("food_ordering", ipa_path=ipa_no_plist, output_root=tmp_path)

    bad_zip = tmp_path / "corrupt.ipa"
    bad_zip.write_bytes(b"not a zip archive")
    with pytest.raises(SymbolToolError, match="invalid IPA zip archive"):
        extract_l1_rn_symbols("food_ordering", ipa_path=bad_zip, output_root=tmp_path)


# ── 3. Dual-Denominator Coverage Metrics (§4.4) ───────────────────────────────


def test_compute_dual_denominator_metrics() -> None:
    metrics = compute_dual_denominator_metrics(
        extracted_identifiers=[
            "home.root",
            "home.ai_prompt.preferences",
            "sentry-logo",
            "preferences.actions.save",
            "home.ai_prompt.preferences",  # duplicate ignored
        ],
        unique_hit_identifiers=[
            "home.ai_prompt.preferences",
            "preferences.actions.save",
            "unextracted.extra.hit",  # must not inflate extracted usability or target coverage
        ],
        target_identifiers=[
            "home.ai_prompt.preferences",
            "home.header.cart",
            "preferences.actions.save",
            "unextracted.extra.hit",
        ],
    )
    assert metrics["extracted_total"] == 4
    assert metrics["unique_hits_total"] == 2
    assert metrics["target_total"] == 4
    assert metrics["covered_target_count"] == 2
    assert metrics["candidate_usability_rate"] == pytest.approx(0.5)
    assert metrics["business_target_coverage"] == pytest.approx(0.5)
    assert metrics["usable_candidates"] == ["home.ai_prompt.preferences", "preferences.actions.save"]
    assert metrics["unmatched_candidates"] == ["home.root", "sentry-logo"]
    assert metrics["covered_targets"] == ["home.ai_prompt.preferences", "preferences.actions.save"]
    assert metrics["missed_targets"] == ["home.header.cart", "unextracted.extra.hit"]


def test_compute_dual_denominator_metrics_handles_empty_denominators() -> None:
    metrics = compute_dual_denominator_metrics([], [], [])
    assert metrics["extracted_total"] == 0
    assert metrics["target_total"] == 0
    assert metrics["candidate_usability_rate"] == 0.0
    assert metrics["business_target_coverage"] == 0.0


# ── 4. CLI Dispatch (`autophone app symbols ...`) ─────────────────────────────


def test_cli_app_symbols_import_l2_and_extract_l1_rn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Keep release lock inside tmp_path so tests never touch repo state.
    monkeypatch.setattr(autophone_cli.common, "STATE_ROOT", tmp_path)
    _make_profile(tmp_path)
    _write_v2_package_manifest(tmp_path)

    manifest_path = tmp_path / "l2.json"
    manifest_path.write_text(
        json.dumps(
            {
                "controls": [
                    {
                        "symbol_id": "ctrl.home.preferences",
                        "kind": "button",
                        "scope": {"screen_id": "S01_home"},
                        "locator": {
                            "kind": "ax_identifier",
                            "identifier": "home.ai_prompt.preferences",
                            "role": "button",
                        },
                        "effects": {"verbs": [], "completeness": "exhaustive"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = autophone_cli.main([
        "app",
        "symbols",
        "import-l2",
        "food_ordering",
        "--manifest",
        str(manifest_path),
        "--build-id",
        "bld-14282f675363",
        "--output-root",
        str(tmp_path),
        "--json",
    ])
    assert rc == 0
    l2_payload = json.loads(capsys.readouterr().out)
    assert l2_payload["app"] == "food_ordering"
    assert l2_payload["artifact_content_digest"] == SHA_V1
    assert l2_payload["control_count"] == 1
    assert l2_payload["claims_written"] == 2
    assert Path(l2_payload["symbol_path"]).is_file()

    # Now run extract-l1-rn on a synthetic IPA into a separate output root.
    l1_root = tmp_path / "l1-out"
    ipa = _make_synthetic_ipa(
        tmp_path / "AppL1.ipa",
        jsbundle_bytes=b'({accessibilityRole:"button",testID:"home.ai_prompt.preferences"})',
    )
    rc_l1 = autophone_cli.main([
        "app",
        "symbols",
        "extract-l1-rn",
        "food_ordering",
        "--ipa",
        str(ipa),
        "--build-id",
        "bld-custom-l1",
        "--default-screen-id",
        "S01_home",
        "--output-root",
        str(l1_root),
        "--json",
    ])
    assert rc_l1 == 0
    l1_payload = json.loads(capsys.readouterr().out)
    assert l1_payload["build_id"] == "bld-custom-l1"
    assert l1_payload["extracted_identifiers"] == ["home.ai_prompt.preferences"]
    assert l1_payload["claims_written"] == 1

    # Human-readable (non---json) mode also works cleanly (idempotent rerun).
    rc_human = autophone_cli.main([
        "app",
        "symbols",
        "extract-l1-rn",
        "food_ordering",
        "--ipa",
        str(ipa),
        "--build-id",
        "bld-custom-l1",
        "--default-screen-id",
        "S01_home",
        "--output-root",
        str(l1_root),
    ])
    assert rc_human == 0
    human_out = capsys.readouterr().out
    assert "symbol_path:" in human_out
    assert "binding_sha256:" in human_out

    # Error exit code 1 with structured JSON stderr when IPA lacks main.jsbundle.
    ipa_missing = _make_synthetic_ipa(tmp_path / "MissingJs.ipa", jsbundle_bytes=None)
    rc_err = autophone_cli.main([
        "app",
        "symbols",
        "extract-l1-rn",
        "food_ordering",
        "--ipa",
        str(ipa_missing),
        "--output-root",
        str(l1_root),
        "--json",
    ])
    assert rc_err == 1
    err_payload = json.loads(capsys.readouterr().err)
    assert err_payload["ok"] is False
    assert err_payload["error"]["code"] == "symbol_tool_error"
    assert "main.jsbundle not found" in err_payload["error"]["message"]


def test_cli_app_symbols_readonly_assets_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(autophone_cli.common, "STATE_ROOT", tmp_path)
    monkeypatch.setenv("AUTOPHONE_READONLY_ASSETS", "1")
    ipa = _make_synthetic_ipa(
        tmp_path / "ReadonlyApp.ipa",
        jsbundle_bytes=b'({testID:"home.ai_prompt.preferences"})',
    )

    # Default asset root mutation is blocked by AUTOPHONE_READONLY_ASSETS=1.
    rc_blocked = autophone_cli.main([
        "app",
        "symbols",
        "extract-l1-rn",
        "food_ordering",
        "--ipa",
        str(ipa),
        "--json",
    ])
    assert rc_blocked == 2
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "readonly_assets"

    # Explicit --output-root remains allowed for dry extraction / inspection.
    out_root = tmp_path / "dry-out"
    rc_ok = autophone_cli.main([
        "app",
        "symbols",
        "extract-l1-rn",
        "food_ordering",
        "--ipa",
        str(ipa),
        "--output-root",
        str(out_root),
        "--json",
    ])
    assert rc_ok == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["extracted_identifiers"] == ["home.ai_prompt.preferences"]


def test_cli_app_symbols_registered_in_public_cli_docs_renderer() -> None:
    import sys

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import generate_public_docs as gen  # noqa: E402

    rendered = gen.render_cli_doc()
    assert "`autophone app symbols`" in rendered
    assert "`autophone app symbols import-l2`" in rendered
    assert "`autophone app symbols extract-l1-rn`" in rendered
