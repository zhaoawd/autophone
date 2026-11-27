"""Immutable submission-time context for every platform run.

The sidecar is the sole source for Registry package projection in run results.
Failure paths return structured unknown values and never consult the current
manifest after submission.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from autophone.packages import store as packages
from autophone.contracts import ContractError, validate_or_raise, write_json
from autophone.util import now_iso, read_json

SIDECAR = "run-submission.json"


@dataclass(frozen=True)
class RegistryBuildRef:
    captured_at: str
    default_slot: str | None
    default_version: str | None
    default_sha256: str | None
    short_version: str | None
    build_version: str | None
    profile_build_label: str | None
    capture_state: str
    unknown_reason: str | None


@dataclass(frozen=True)
class RunSubmission:
    run_id: str
    app: str
    created_at: str
    registry_build_ref: RegistryBuildRef
    # What the platform last verifiably installed on the Target, frozen at
    # submission.  Distinct from ``registry_build_ref`` above, which is the
    # Asset catalog's default *selection*: a run is about the installed Build,
    # not about which slot the manifest happens to default to.
    installed_build_ref: dict[str, Any] | None = None
    # The receipt's ``verified`` flag at submission; None for sidecars written
    # before it was frozen.  ``False`` (``--no-verify`` install) disqualifies the
    # ref as comparison basis.
    installed_build_verified: bool | None = None
    registry_candidate_ref: dict[str, Any] | None = None
    # Selected base for an explicit candidate, independent of prior installed facts.
    candidate_build_ref: dict[str, Any] | None = None
    schema_version: str = "v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _unknown(run_id: str, app: str, reason: str) -> RunSubmission:
    captured_at = now_iso()
    return RunSubmission(
        run_id=run_id,
        app=app,
        created_at=captured_at,
        registry_build_ref=RegistryBuildRef(
            captured_at=captured_at,
            default_slot=None,
            default_version=None,
            default_sha256=None,
            short_version=None,
            build_version=None,
            profile_build_label=None,
            capture_state="unknown",
            unknown_reason=reason,
        ),
    )


def _from_dict(raw: dict[str, Any]) -> RunSubmission:
    # Historical sidecars used ``active_*`` for the Asset selection.  Normalize
    # them in memory; new sidecars only write the narrower ``default_*`` names.
    raw = dict(raw)
    legacy_ref = raw.get("registry_build_ref")
    if isinstance(legacy_ref, dict) and "default_slot" not in legacy_ref:
        ref_copy = dict(legacy_ref)
        for suffix in ("slot", "version", "sha256"):
            ref_copy[f"default_{suffix}"] = ref_copy.pop(f"active_{suffix}", None)
        raw["registry_build_ref"] = ref_copy
    validate_or_raise("run-submission.v1.json", raw)
    ref = raw["registry_build_ref"]
    return RunSubmission(
        schema_version=str(raw["schema_version"]),
        run_id=str(raw["run_id"]),
        app=str(raw["app"]),
        created_at=str(raw["created_at"]),
        registry_build_ref=RegistryBuildRef(**ref),
        registry_candidate_ref=deepcopy(raw.get("registry_candidate_ref")),
        candidate_build_ref=deepcopy(raw.get("candidate_build_ref")),
        installed_build_ref=raw.get("installed_build_ref") or None,
        installed_build_verified=(
            raw["installed_build_verified"]
            if isinstance(raw.get("installed_build_verified"), bool) else None
        ),
    )


def initialize(run_dir: Path, spec: dict[str, Any], profile: Any) -> RunSubmission:
    """Capture Registry state once; an existing valid sidecar is immutable."""
    path = run_dir / SIDECAR
    expected_run_id = str(spec.get("run_id") or "")
    expected_app = str(spec.get("app") or "")
    if path.exists():
        try:
            existing = _from_dict(read_json(path) or {})
        except Exception as exc:  # noqa: BLE001 - do not replace damaged evidence
            raise ContractError(f"existing {SIDECAR} is invalid: {exc}") from exc
        if existing.run_id != expected_run_id or existing.app != expected_app:
            raise ContractError(
                f"existing {SIDECAR} identity mismatch: "
                f"{existing.run_id}/{existing.app} != {expected_run_id}/{expected_app}"
            )
        if existing.registry_candidate_ref != spec.get("candidate_ref"):
            raise ContractError("existing run-submission candidate identity mismatch")
        return existing

    captured_at = now_iso()
    try:
        manifest = packages.load_manifest(
            expected_app,
            root=profile.host_state_root,
            code_root=profile.root,
        )
        default_slot = _text(manifest.get("default_slot"))
        default_entry = next(
            (
                row for row in manifest.get("packages") or []
                if _text(row.get("slot")) == default_slot
            ),
            None,
        )
        if default_slot is None:
            state, reason = "unknown", "registry_default_slot_missing"
        elif default_entry is None:
            state, reason = "unknown", "registry_default_entry_missing"
        else:
            state, reason = "captured", None
        ref = RegistryBuildRef(
            captured_at=captured_at,
            default_slot=default_slot,
            default_version=_text((default_entry or {}).get("version")),
            default_sha256=_text((default_entry or {}).get("sha256")),
            short_version=_text((default_entry or {}).get("short_version")),
            build_version=_text((default_entry or {}).get("build_version")),
            profile_build_label=_text(getattr(profile, "build_label", None)),
            capture_state=state,
            unknown_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - submission remains auditable as unknown
        ref = RegistryBuildRef(
            captured_at=captured_at,
            default_slot=None,
            default_version=None,
            default_sha256=None,
            short_version=None,
            build_version=None,
            profile_build_label=_text(getattr(profile, "build_label", None)),
            capture_state="unknown",
            unknown_reason=f"registry_capture_failed:{type(exc).__name__}",
        )
    try:
        receipt = packages.load_target_receipt(
            expected_app, root=profile.host_state_root,
        )
        installed_ref = dict(receipt.get("build_ref") or {}) if receipt else None
        verified = receipt.get("verified") if receipt else None
        installed_verified = verified if isinstance(verified, bool) else None
    except Exception:  # noqa: BLE001 - absence is structured evidence, not a stop
        installed_ref = None
        installed_verified = None
    candidate = spec.get("candidate_ref") or {}
    selected_base = candidate.get("build_ref" if candidate.get("candidate_kind") == "ipa" else "native_shell")
    build_fields = ("app", "slot", "build_id", "artifact_sha256", "short_version", "build_version",
                    "source_revision", "resolution_source")
    candidate_base = ({key: deepcopy(value) for key, value in selected_base.items() if key in build_fields}
                      if isinstance(selected_base, Mapping) else None)
    submission = RunSubmission(
        run_id=expected_run_id,
        app=expected_app,
        created_at=captured_at,
        registry_build_ref=ref,
        registry_candidate_ref=deepcopy(spec.get("candidate_ref")),
        candidate_build_ref=candidate_base,
        installed_build_ref=installed_ref,
        installed_build_verified=installed_verified if installed_ref else None,
    )
    validate_or_raise("run-submission.v1.json", submission.to_dict())
    write_json(path, submission.to_dict())
    return submission


def load(run_dir: Path, expected_run_id: str, expected_app: str) -> RunSubmission:
    """Load a sidecar; missing/corrupt state becomes structured unknown."""
    path = run_dir / SIDECAR
    try:
        raw = read_json(path)
        if not isinstance(raw, dict):
            return _unknown(expected_run_id, expected_app, "run_submission_missing")
        submission = _from_dict(raw)
    except (OSError, json.JSONDecodeError, ContractError, TypeError, ValueError) as exc:
        return _unknown(
            expected_run_id,
            expected_app,
            f"run_submission_invalid:{type(exc).__name__}",
        )
    if submission.run_id != expected_run_id or submission.app != expected_app:
        return _unknown(expected_run_id, expected_app, "run_submission_identity_mismatch")
    return submission


# Build comparison uses the Run-frozen candidate base when explicitly selected;
# legacy Runs use their frozen ``installed_build_ref`` (BL-190).  A ``match`` needs build-marker evidence: each Run's
# ``world-identity.json`` compares the installed app's build marker bytes with
# the marker expected for that Build (``subject_build_ref.state``: observed =
# equal, mismatch = different).  The Info.plist version tuple is compared as
# well, but version equality alone never yields ``match`` because distinct
# Builds may share ``short_version`` + ``build_version``.
#
# BL-241 step two: a Build whose ``source_revision.kind`` is ``unknown``
# (third-party IPA, no build marker) is compared at ``info_plist`` granularity:
# the device Info.plist bytes against the Info.plist inside the locally
# re-hashed basis IPA (``subject_build_ref.info_plist``), and ``match`` also
# needs those bytes to be unique among the App's Package Store Builds
# (``subject_build_ref.info_plist_basis.uniqueness=unique``).  Builds of any
# other kind keep the build-marker rule only.
INSTALLED_BUILD_BASIS = "installed_build_ref"
BUILD_MARKER_GRANULARITY = "build_marker"
INFO_PLIST_GRANULARITY = "info_plist"
VERSION_GRANULARITY = "version"
WORLD_IDENTITY = "world-identity.json"
_INSTALLED_REQUIRED = ("build_id", "artifact_sha256", "short_version", "build_version")
# Basis fields a ``kind=unknown`` Build may take, read only, from the
# re-hashed IPA Info.plist when its Build Record / receipt holds null.
_DERIVABLE_FROM_INFO_PLIST = {
    "short_version": "ipa_short_version",
    "build_version": "ipa_build_version",
}
_OBSERVATION_KEYS = ("bundle_id", "short_version", "build_version", "pid", "observed_at")


def _source_revision_kind(ref: Mapping[str, Any]) -> str | None:
    revision = ref.get("source_revision")
    return revision.get("kind") if isinstance(revision, Mapping) else None


def _unknown_kind_basis(
    installed: Mapping[str, Any], world_subject: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """The world identity's Info.plist basis for this ``kind=unknown`` Build, if usable."""
    if _source_revision_kind(installed) != "unknown" or not isinstance(world_subject, Mapping):
        return None
    basis = world_subject.get("info_plist_basis")
    if (
        not isinstance(basis, Mapping)
        or basis.get("build_id") != installed.get("build_id")
        or basis.get("reason") is not None
        or basis.get("source_revision_kind") != "unknown"
    ):
        return None
    return basis


def installed_comparison_basis(
    submission: RunSubmission,
    *,
    world_subject: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The installed Build ref usable as comparison basis, or the reason it is not.

    For a ``source_revision.kind=unknown`` Build, a null ``short_version`` or
    ``build_version`` is taken from the Run's world identity
    ``info_plist_basis`` (the Info.plist of the locally re-hashed IPA, BL-241).
    The value only lives in the returned basis; the frozen submission, the
    receipt and the Build Record are never rewritten.
    """
    installed = submission.candidate_build_ref or submission.installed_build_ref
    if not isinstance(installed, Mapping) or not installed:
        return None, "installed_build_ref_missing"
    missing = [key for key in _INSTALLED_REQUIRED if installed.get(key) in (None, "")]
    if missing:
        basis = _unknown_kind_basis(installed, world_subject)
        derived = {
            key: basis.get(_DERIVABLE_FROM_INFO_PLIST[key])
            for key in missing if basis is not None and key in _DERIVABLE_FROM_INFO_PLIST
        }
        if len(derived) != len(missing) or any(value in (None, "") for value in derived.values()):
            return None, "installed_build_ref_incomplete"
        installed = {**installed, **derived}
    if submission.candidate_build_ref is None and submission.installed_build_verified is False:
        return None, "installed_build_ref_unverified"
    return dict(installed), None


def load_world_subject_build_ref(run_dir: Path) -> dict[str, Any] | None:
    """``subject_build_ref`` from the Run's frozen world identity, if recorded."""
    try:
        raw = read_json(run_dir / WORLD_IDENTITY)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    value = raw.get("subject_build_ref") if isinstance(raw, dict) else None
    return dict(value) if isinstance(value, dict) else None


def project_device_observation(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Narrow a ``build-identity.json`` device observation to the run-result shape."""
    if not isinstance(raw, Mapping) or not raw:
        return None
    projected: dict[str, Any] = {}
    for key in _OBSERVATION_KEYS:
        value = raw.get(key)
        if key == "pid":
            projected[key] = int(value) if isinstance(value, int) and not isinstance(value, bool) else None
        else:
            projected[key] = str(value) if value not in (None, "") else None
    return projected


def _version_comparison(
    installed: Mapping[str, Any], observation: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    if not observation:
        return "not_observed", "device_observation_missing"
    pairs = [(str(installed[key]), observation.get(key)) for key in ("short_version", "build_version")]
    if any(seen not in (None, "") and str(seen) != expected for expected, seen in pairs):
        return "mismatch", "device_version_tuple_mismatch"
    if any(seen in (None, "") for _expected, seen in pairs):
        return "not_observed", "device_version_tuple_incomplete"
    return "match", None


def _marker_comparison(
    installed: Mapping[str, Any], subject: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    if not isinstance(subject, Mapping):
        return "not_recorded", "world_identity_missing"
    frozen = subject.get("installed_build_ref")
    frozen_id = frozen.get("build_id") if isinstance(frozen, Mapping) else None
    if frozen_id != installed["build_id"]:
        return "not_comparable", "world_identity_build_ref_differs"
    state = str(subject.get("state") or "unprobed")
    reason = subject.get("reason")
    if state in ("observed", "mismatch"):
        return state, str(reason) if reason else None
    return state, str(reason) if reason else f"build_marker_{state}"


def _info_plist_comparison(
    installed: Mapping[str, Any], subject: Mapping[str, Any] | None,
) -> tuple[str, str | None] | None:
    """``info_plist`` verdict for a ``kind=unknown`` Build; None when the rule does not apply.

    The rule applies when the installed ref is ``kind=unknown`` and the world
    identity recorded an Info.plist comparison against this same Build.
    """
    if _source_revision_kind(installed) != "unknown" or not isinstance(subject, Mapping):
        return None
    record = subject.get("info_plist")
    if not isinstance(record, Mapping) or record.get("basis_build_id") != installed["build_id"]:
        return None
    comparison = record.get("comparison")
    if comparison == "different":
        return "mismatch", "installed_info_plist_mismatch"
    if comparison != "equal":
        return "not_observed", f"info_plist_not_compared:{record.get('reason')}"
    basis = subject.get("info_plist_basis")
    if not isinstance(basis, Mapping) or basis.get("build_id") != installed["build_id"]:
        return "not_observed", "info_plist_basis_missing"
    if basis.get("reason") is not None:
        return "not_observed", str(basis["reason"])
    if basis.get("source_revision_kind") != "unknown":
        return "not_observed", "info_plist_basis_source_revision_not_unknown"
    if basis.get("info_plist_sha256") != record.get("expected_sha256"):
        return "not_observed", "info_plist_basis_inconsistent"
    uniqueness = basis.get("uniqueness")
    if uniqueness == "unique":
        return "match", None
    if uniqueness == "ambiguous":
        return "not_observed", "info_plist_ambiguous"
    return "not_observed", "info_plist_uniqueness_unverified"


def compare_installed_build(
    installed: Mapping[str, Any] | None,
    observation: Mapping[str, Any] | None,
    *,
    basis_reason: str | None = None,
    world_subject: Mapping[str, Any] | None = None,
    probe_match: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the frozen installed Build with the Run's device evidence.

    - any mismatch (build marker, version tuple, or a Frida/UDS tuple
      disagreement recorded in ``build-identity.json``) -> ``mismatch``;
    - an Info.plist difference for a ``kind=unknown`` Build -> ``mismatch``
      (granularity ``info_plist``, BL-241);
    - otherwise build marker ``observed`` -> ``match`` (granularity
      ``build_marker``);
    - otherwise, for a ``kind=unknown`` Build, equal Info.plist bytes that are
      unique in the App's Package Store -> ``match`` (granularity
      ``info_plist``);
    - otherwise ``not_observed``: version equality alone is recorded in
      ``version_comparison`` but is not reported as a match.
    """
    result: dict[str, Any] = {
        "build_identity_match": "not_observed",
        "comparison_basis": None,
        "comparison_build_id": None,
        "comparison_granularity": None,
        "comparison_reason": None,
        "version_comparison": None,
        "build_marker_comparison": None,
    }
    if installed is None:
        result["comparison_reason"] = basis_reason or "installed_build_ref_missing"
        return result
    version, version_reason = _version_comparison(installed, observation)
    marker, marker_reason = _marker_comparison(installed, world_subject)
    info_plist = _info_plist_comparison(installed, world_subject)
    plist_state, plist_reason = info_plist or (None, None)
    result.update(
        comparison_basis=INSTALLED_BUILD_BASIS,
        comparison_build_id=str(installed["build_id"]),
        version_comparison=version,
        build_marker_comparison=marker,
    )
    # The build-identity.json probe adds one fact the two comparisons above do
    # not cover: a Frida process tuple that disagrees with the UDS Info.plist
    # tuple.  Its Registry-row reasons are about the default selection, which
    # the world identity already compares.
    probe = probe_match or {}
    probe_state = (
        "mismatch"
        if (
            probe.get("state") == "mismatch"
            and probe.get("match_basis")
            in {"uds_frida_bundle_tuple", "installed_build_ref_build_marker"}
        )
        else ""
    )
    if marker == "mismatch":
        by_marker = marker_reason == "installed_build_marker_mismatch"
        result.update(
            build_identity_match="mismatch",
            comparison_granularity=BUILD_MARKER_GRANULARITY if by_marker else VERSION_GRANULARITY,
            comparison_reason=marker_reason or "world_subject_build_ref_mismatch",
        )
    elif version == "mismatch":
        result.update(
            build_identity_match="mismatch",
            comparison_granularity=VERSION_GRANULARITY,
            comparison_reason=version_reason,
        )
    elif plist_state == "mismatch":
        result.update(
            build_identity_match="mismatch",
            comparison_granularity=INFO_PLIST_GRANULARITY,
            comparison_reason=plist_reason,
        )
    elif probe_state == "mismatch":
        granularity = (
            BUILD_MARKER_GRANULARITY
            if probe.get("match_basis") == "installed_build_ref_build_marker"
            else VERSION_GRANULARITY
        )
        result.update(
            build_identity_match="mismatch",
            comparison_granularity=granularity,
            comparison_reason=f"build_identity_probe_mismatch:{probe.get('reason')}",
        )
    elif marker == "observed":
        result.update(
            build_identity_match="match",
            comparison_granularity=BUILD_MARKER_GRANULARITY,
        )
    elif plist_state == "match":
        result.update(
            build_identity_match="match",
            comparison_granularity=INFO_PLIST_GRANULARITY,
        )
    elif info_plist is not None:
        result["comparison_reason"] = plist_reason
    else:
        result["comparison_reason"] = marker_reason
    return result


def package_projection(
    run_dir: Path,
    *,
    expected_run_id: str,
    expected_app: str,
    device_short_version: str | None = None,
    device_observation: Mapping[str, Any] | None = None,
    probe_match: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    submission = load(run_dir, expected_run_id, expected_app)
    ref = submission.registry_build_ref
    world_subject = load_world_subject_build_ref(run_dir)
    installed, basis_reason = installed_comparison_basis(submission, world_subject=world_subject)
    observation = project_device_observation(device_observation)
    comparison = compare_installed_build(
        installed,
        observation,
        basis_reason=basis_reason,
        world_subject=world_subject,
        probe_match=probe_match,
    )
    if submission.candidate_build_ref is not None and comparison.get("comparison_basis") is not None:
        comparison["comparison_basis"] = "candidate_build_ref"
    return {
        # Installed Build identity + device observation stay separate keys: a
        # missing observation is reported as ``not_observed``, never as a match.
        "registry_build_ref": submission.candidate_build_ref or submission.installed_build_ref or None,
        "device_build_observation": observation,
        **comparison,
        # ``default_*`` stays the legacy (v1) Registry default selection; it is
        # not the comparison basis and is empty for Package Store v2 manifests.
        "default_slot": ref.default_slot,
        "default_version": ref.default_version,
        "default_sha256": ref.default_sha256,
        "short_version": ref.short_version,
        "build_version": ref.build_version,
        "device_version": device_short_version,
        "profile_build_label": ref.profile_build_label,
        "snapshot_source": "run_submission" if ref.capture_state == "captured" else "unknown",
        "snapshot_unknown_reason": ref.unknown_reason,
    }
