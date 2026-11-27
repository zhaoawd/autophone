"""Contract tests for Milestone M1: app-symbols.v1, app-profile.v1, and operation-ax-checkpoint.v2."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from autophone.assets.apps import (
    AppProfile,
    AxCapability,
    DEFAULT_AX_BACKEND,
    DEFAULT_TRIAL_DATA_ISOLATION,
    load_app_profile,
)
from autophone.contracts import ContractError, load_schema, validate_or_raise

SHA_A = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
SHA_B = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_C = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHA_D = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


def _valid_ipa_symbols_doc() -> dict[str, Any]:
    """Exact structure from docs/concepts/app-symbols-v1.md Section 3.1."""
    return yaml.safe_load(
        f"""
schema: autophone.app-symbols/1
app: food_ordering
artifact_identity:
  kind: ipa_build
  bundle_id: "com.autophone.foodorderingdemo.v2"
  short_version: "1.0.0"
  build_version: "1"
  build_id: "bld-14282f675363"
  artifact_sha256: "{SHA_A}"
controls:
  ctrl.home.preferences:
    kind: button
    scope:
      screen_id: "S01_home"
      region: [0.05, 0.15, 0.95, 0.90]
    locator:
      kind: ax_identifier
      identifier: "home.ai_prompt.preferences"
      role: button
      enabled_only: true
      require_unique: true
    effects:
      verbs: []
      completeness: exhaustive
"""
    )


def _valid_rn_candidate_symbols_doc() -> dict[str, Any]:
    doc = _valid_ipa_symbols_doc()
    doc["artifact_identity"] = {
        "kind": "rn_candidate",
        "bundle_id": "com.autophone.foodorderingdemo.v2",
        "short_version": "1.0.0",
        "build_version": "1",
        "base_ipa_sha256": SHA_A,
        "main_jsbundle_sha256": SHA_B,
        "assets_tree_sha256": SHA_C,
    }
    doc["controls"]["ctrl.checkout.submit"] = {
        "kind": "button",
        "scope": {"screen_id": "S04_checkout"},
        "locator": {
            "kind": "ax_identifier",
            "identifier": "checkout.submit",
            "role": None,
            "enabled_only": True,
            "require_unique": True,
        },
        "effects": {
            "verbs": ["place_order", "pay"],
            "completeness": "lower_bound",
        },
    }
    return doc


def _base_profile_raw() -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "app": "food_ordering",
        "bundle_id": "com.autophone.foodorderingdemo.v2",
        "display_name": "Food Ordering",
    }


def _valid_build_identity_v1() -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "run_id": "run_20261126_01",
        "app": "food_ordering",
        "identity_state": "confirmed",
        "identity_source": "combined",
        "subject_build_key": "com.autophone.foodorderingdemo.v2:1.0.0:1",
        "device_observation": {
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
        },
        "match": {"status": "match"},
        "subject_epochs": [],
        "updated_at": "2026-11-26T12:00:00Z",
    }


def _valid_build_identity_v2(*, with_candidate: bool = True) -> dict[str, Any]:
    candidate: dict[str, Any] | None = None
    if with_candidate:
        candidate = {
            "target_fingerprint": f"sha256:{SHA_D}",
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "base_build_id": "bld-14282f675363",
            "base_ipa_sha256": SHA_A,
            "native_runtime_id": SHA_D,
            "update_source": "ota",
            "active_update_id": "upd-20261126-01",
            "main_jsbundle_sha256": SHA_B,
            "assets_tree_sha256": SHA_C,
            "evidence_role": "identity_only",
            "candidate_kind": "rn_update",
        }
    return {
        "schema_version": "v2",
        "run_id": "run_20261126_01",
        "app": "food_ordering",
        "identity_state": "confirmed",
        "identity_source": "combined",
        "subject_build_key": "com.autophone.foodorderingdemo.v2:1.0.0:1",
        "device_observation": {
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
        },
        "match": {"status": "match"},
        "subject_epochs": [
            {
                "epoch_ref": "epoch-1",
                "pid": 1234,
                "bundle_id": "com.autophone.foodorderingdemo.v2",
                "short_version": "1.0.0",
                "build_version": "1",
                "started_at": "2026-11-26T12:00:00Z",
                "source": "rn_candidate" if with_candidate else "uds_info_plist",
                "superseded_at": None,
                **(
                    {
                        "candidate_identity_ref": f"candidate-identities/{SHA_B}.json",
                        "candidate_identity_sha256": SHA_B,
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


def _valid_ax_checkpoint_v2(build_identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "v2",
        "identity": {
            "run_id": "run_20261126_01",
            "app": "food_ordering",
            "build": "1.0.0-1",
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "operation": {"id": "OP1", "name": "open_preferences", "revision": 1},
        },
        "checkpoint": {
            "expected": "S01_home",
            "matched": "S01_home",
            "shot": "shots/R3_01.jpg",
            "attempt": 1,
        },
        "shot_sha256": SHA_A,
        "snapshot": {
            "schema": "autophone.ax/1",
            "evidence_protocol": 1,
            "request_id": "req-001",
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
        },
        "build_identity": build_identity,
        "build_evidence": {
            "package": {
                "registry_build_ref": {"app": "food_ordering", "build_id": "bld-14282f675363"},
                "device_build_observation": {
                    "bundle_id": "com.autophone.foodorderingdemo.v2",
                    "short_version": "1.0.0",
                    "build_version": "1",
                },
                "build_identity_match": "match",
                "comparison_granularity": "info_plist",
                "comparison_build_id": "bld-14282f675363",
            },
            "sources": {
                "run-submission.json": {"path": "run-submission.json", "sha256": SHA_B},
                "world-identity.json": {"path": "world-identity.json", "sha256": SHA_C},
            },
        },
    }


# ── 1. app-symbols.v1.json ────────────────────────────────────────────────────


@pytest.mark.contract
def test_app_symbols_v1_accepts_spec_ipa_build_and_rn_candidate() -> None:
    load_schema("app-symbols.v1.json")
    validate_or_raise("app-symbols.v1.json", _valid_ipa_symbols_doc())
    validate_or_raise("app-symbols.v1.json", _valid_rn_candidate_symbols_doc())

    # Unknown completeness and omitted optional role/region are also valid (L1 RN draft).
    l1_doc = _valid_ipa_symbols_doc()
    l1_doc["controls"]["ctrl.home.preferences"]["scope"].pop("region")
    l1_doc["controls"]["ctrl.home.preferences"]["locator"].pop("role")
    l1_doc["controls"]["ctrl.home.preferences"]["effects"]["completeness"] = "unknown"
    validate_or_raise("app-symbols.v1.json", l1_doc)


@pytest.mark.contract
@pytest.mark.parametrize(
    "mutator",
    [
        # Top-level target_fingerprint / relation_claims / extra property
        lambda d: d.__setitem__("target_fingerprint", f"sha256:{SHA_A}"),
        lambda d: d.__setitem__("relation_claims", []),
        lambda d: d.__setitem__("extra_field", "nope"),
        # target_fingerprint inside artifact_identity (ipa_build)
        lambda d: d["artifact_identity"].__setitem__("target_fingerprint", f"sha256:{SHA_A}"),
        # relation_claims inside artifact_identity or controls or nested control blocks
        lambda d: d["artifact_identity"].__setitem__("relation_claims", []),
        lambda d: d["controls"].__setitem__("relation_claims", []),
        lambda d: d["controls"].__setitem__(
            "relation_claims", copy.deepcopy(d["controls"]["ctrl.home.preferences"])
        ),
        lambda d: d["controls"]["ctrl.home.preferences"].__setitem__("relation_claims", []),
        lambda d: d["controls"]["ctrl.home.preferences"]["scope"].__setitem__("relation_claims", []),
        lambda d: d["controls"]["ctrl.home.preferences"]["locator"].__setitem__("relation_claims", []),
        lambda d: d["controls"]["ctrl.home.preferences"]["effects"].__setitem__("relation_claims", []),
        # Wrong schema const / empty app
        lambda d: d.__setitem__("schema", "autophone.app-symbols/2"),
        lambda d: d.__setitem__("app", ""),
        # Bad completeness / duplicate verbs / empty verb
        lambda d: d["controls"]["ctrl.home.preferences"]["effects"].__setitem__("completeness", "partial"),
        lambda d: d["controls"]["ctrl.home.preferences"]["effects"].__setitem__("verbs", ["pay", "pay"]),
        lambda d: d["controls"]["ctrl.home.preferences"]["effects"].__setitem__("verbs", [""]),
        # Bad sha256 variants
        lambda d: d["artifact_identity"].__setitem__("artifact_sha256", "not-a-sha256"),
        lambda d: d["artifact_identity"].__setitem__("artifact_sha256", SHA_A.upper()),
        lambda d: d["artifact_identity"].__setitem__("artifact_sha256", f"sha256:{SHA_A}"),
        # Missing required ipa_build field / mixing rn_candidate field into ipa_build
        lambda d: d["artifact_identity"].pop("build_id"),
        lambda d: d["artifact_identity"].__setitem__("main_jsbundle_sha256", SHA_B),
        # Bad locator kind / non-bool flags / bad region
        lambda d: d["controls"]["ctrl.home.preferences"]["locator"].__setitem__("kind", "ocr_text"),
        lambda d: d["controls"]["ctrl.home.preferences"]["locator"].__setitem__("require_unique", "true"),
        lambda d: d["controls"]["ctrl.home.preferences"]["locator"].__setitem__("enabled_only", 1),
        lambda d: d["controls"]["ctrl.home.preferences"]["scope"].__setitem__("region", [0.0, 0.0, 1.0]),
        lambda d: d["controls"]["ctrl.home.preferences"]["scope"].__setitem__("region", [-0.1, 0.0, 1.0, 1.0]),
        lambda d: d["controls"]["ctrl.home.preferences"]["scope"].__setitem__("region", [0.0, 0.0, 1.2, 1.0]),
    ],
)
def test_app_symbols_v1_rejects_invalid_ipa_documents(mutator: Any) -> None:
    doc = _valid_ipa_symbols_doc()
    mutator(doc)
    with pytest.raises(ContractError):
        validate_or_raise("app-symbols.v1.json", doc)


@pytest.mark.contract
@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d["artifact_identity"].__setitem__("target_fingerprint", f"sha256:{SHA_D}"),
        lambda d: d["artifact_identity"].pop("base_ipa_sha256"),
        lambda d: d["artifact_identity"].pop("main_jsbundle_sha256"),
        lambda d: d["artifact_identity"].pop("assets_tree_sha256"),
        lambda d: d["artifact_identity"].__setitem__("main_jsbundle_sha256", "deadbeef"),
        lambda d: d["artifact_identity"].__setitem__("artifact_sha256", SHA_A),
        lambda d: d["artifact_identity"].__setitem__("build_id", "bld-14282f675363"),
    ],
)
def test_app_symbols_v1_rejects_invalid_rn_candidate_documents(mutator: Any) -> None:
    doc = _valid_rn_candidate_symbols_doc()
    mutator(doc)
    with pytest.raises(ContractError):
        validate_or_raise("app-symbols.v1.json", doc)


# ── 2. app-profile.v1.json & AppProfile ───────────────────────────────────────


@pytest.mark.contract
def test_app_profile_v1_defaults_and_explicit_values(tmp_path: Path) -> None:
    # Backward-compatible profile omitting capabilities.ax.backend and trial_data_isolation.
    minimal = _base_profile_raw()
    validate_or_raise("app-profile.v1.json", minimal)
    app_dir = tmp_path / "apps" / "food_ordering"
    app_dir.mkdir(parents=True)
    (app_dir / "profile.yaml").write_text(yaml.safe_dump(minimal), encoding="utf-8")

    loaded = load_app_profile("food_ordering", root=tmp_path)
    assert loaded.trial_data_isolation == DEFAULT_TRIAL_DATA_ISOLATION == "unisolated"
    assert loaded.ax_backend == DEFAULT_AX_BACKEND == "guest_axruntime"
    assert loaded.ax_enabled is False
    assert loaded.ax_capability == AxCapability(enabled=False, backend="guest_axruntime")

    # Profile with capabilities.ax.enabled=true and no backend key defaults to guest_axruntime.
    default_ax = _base_profile_raw()
    default_ax["capabilities"] = {"ax": {"enabled": True}}
    validate_or_raise("app-profile.v1.json", default_ax)
    (app_dir / "profile.yaml").write_text(yaml.safe_dump(default_ax), encoding="utf-8")
    loaded_default_ax = load_app_profile("food_ordering", root=tmp_path)
    assert loaded_default_ax.ax_enabled is True
    assert loaded_default_ax.ax_backend == "guest_axruntime"
    assert loaded_default_ax.ax_capability == AxCapability(enabled=True, backend="guest_axruntime")
    assert loaded_default_ax.trial_data_isolation == "unisolated"

    # Explicit frida_inproc opt-in + isolated values remain fully supported.
    explicit_frida = _base_profile_raw()
    explicit_frida["trial_data_isolation"] = "isolated"
    explicit_frida["capabilities"] = {"ax": {"enabled": True, "backend": "frida_inproc"}}
    validate_or_raise("app-profile.v1.json", explicit_frida)
    (app_dir / "profile.yaml").write_text(yaml.safe_dump(explicit_frida), encoding="utf-8")
    loaded_frida = load_app_profile("food_ordering", root=tmp_path)
    assert loaded_frida.trial_data_isolation == "isolated"
    assert loaded_frida.ax_enabled is True
    assert loaded_frida.ax_backend == "frida_inproc"
    assert loaded_frida.ax_capability == AxCapability(enabled=True, backend="frida_inproc")

    # Explicit guest_axruntime + isolated values.
    explicit = _base_profile_raw()
    explicit["trial_data_isolation"] = "isolated"
    explicit["capabilities"] = {"ax": {"enabled": True, "backend": "guest_axruntime"}}
    validate_or_raise("app-profile.v1.json", explicit)
    (app_dir / "profile.yaml").write_text(yaml.safe_dump(explicit), encoding="utf-8")
    loaded_explicit = load_app_profile("food_ordering", root=tmp_path)
    assert loaded_explicit.trial_data_isolation == "isolated"
    assert loaded_explicit.ax_enabled is True
    assert loaded_explicit.ax_backend == "guest_axruntime"
    assert loaded_explicit.ax_capability == AxCapability(enabled=True, backend="guest_axruntime")


@pytest.mark.contract
def test_app_profile_v1_rejects_invalid_ax_backend_and_trial_data_isolation(tmp_path: Path) -> None:
    bad_isolation = _base_profile_raw()
    bad_isolation["trial_data_isolation"] = "sandboxed"
    with pytest.raises(ContractError):
        validate_or_raise("app-profile.v1.json", bad_isolation)

    bad_backend = _base_profile_raw()
    bad_backend["capabilities"] = {"ax": {"enabled": True, "backend": "xcuitest"}}
    with pytest.raises(ContractError):
        validate_or_raise("app-profile.v1.json", bad_backend)

    # Direct AppProfile construction also fails closed on invalid raw values.
    with pytest.raises(ValueError, match="trial_data_isolation"):
        _ = AppProfile(raw=bad_isolation, root=tmp_path).trial_data_isolation
    with pytest.raises(ValueError, match="capabilities.ax.backend"):
        _ = AppProfile(raw=bad_backend, root=tmp_path).ax_backend


# ── 3. operation-ax-checkpoint.v2.json ────────────────────────────────────────


@pytest.mark.contract
@pytest.mark.parametrize("backend", [None, "frida_inproc", "guest_axruntime"])
def test_operation_ax_checkpoint_v2_accepts_build_identity_v1_and_v2_and_ax_backend(
    backend: str | None,
) -> None:
    for build_identity in (
        _valid_build_identity_v1(),
        _valid_build_identity_v2(with_candidate=False),
        _valid_build_identity_v2(with_candidate=True),
    ):
        payload = _valid_ax_checkpoint_v2(build_identity)
        if backend is not None:
            payload["ax_backend"] = backend
            payload["snapshot"]["ax_backend"] = backend
        validate_or_raise("operation-ax-checkpoint.v2.json", payload)


@pytest.mark.contract
def test_operation_ax_checkpoint_v2_rejects_bad_build_identity_and_bad_ax_backend() -> None:
    # Bad ax_backend on top-level checkpoint payload.
    bad_top = _valid_ax_checkpoint_v2(_valid_build_identity_v2())
    bad_top["ax_backend"] = "unknown_backend"
    with pytest.raises(ContractError):
        validate_or_raise("operation-ax-checkpoint.v2.json", bad_top)

    # Bad ax_backend inside snapshot payload.
    bad_snap = _valid_ax_checkpoint_v2(_valid_build_identity_v2())
    bad_snap["snapshot"]["ax_backend"] = "unknown_backend"
    with pytest.raises(ContractError):
        validate_or_raise("operation-ax-checkpoint.v2.json", bad_snap)

    # Invalid build_identity v2 (missing candidate_identity required field).
    bad_v2_bi = _valid_build_identity_v2(with_candidate=True)
    del bad_v2_bi["candidate_identity"]
    with pytest.raises(ContractError):
        validate_or_raise("operation-ax-checkpoint.v2.json", _valid_ax_checkpoint_v2(bad_v2_bi))

    # Unknown build_identity schema_version.
    bad_ver_bi = _valid_build_identity_v1()
    bad_ver_bi["schema_version"] = "v3"
    with pytest.raises(ContractError):
        validate_or_raise("operation-ax-checkpoint.v2.json", _valid_ax_checkpoint_v2(bad_ver_bi))
