"""Build identity probe and SubjectEpoch persistence.

Registry SHA values remain host registry facts.  Device identity is the
bundle/short/build version tuple observed from the installed Info.plist or the
attached process.  The module never labels a Registry SHA as device-confirmed.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from autophone.contracts import validate_or_raise, write_json
from autophone.assets.plist import bundle_identity, parse_plist
from autophone.util import now_iso, read_json

SIDECAR = "build-identity.json"


class SubmissionBuildRef(Protocol):
    """提交时冻结的 Registry 选择里，本模块实际读到的字段。

    `autophone.execution.run_submission.RegistryBuildRef` 是本 Protocol 的实现；
    这里结构化声明而不 import 平台层，让 `assets` 不再反向依赖 `platform`
    （BL-003 判据 A / `platform↔assets` 双向边）。
    """

    default_slot: str | None
    default_version: str | None
    default_sha256: str | None
    short_version: str | None
    build_version: str | None


@dataclass(frozen=True)
class DeviceBuildObservation:
    bundle_id: str
    short_version: str | None
    build_version: str | None
    install_path: str
    pid: int
    observed_at: str
    foreground_visible: bool | None
    foreground_evidence: str | None
    producer_liveness: str
    build_marker_sha256: str | None = None

    @property
    def subject_build_key(self) -> str | None:
        return subject_build_key(
            self.bundle_id, self.short_version, self.build_version
        )


@dataclass(frozen=True)
class SubjectEpoch:
    epoch_ref: str
    pid: int
    bundle_id: str
    short_version: str | None
    build_version: str | None
    started_at: str
    source: str
    superseded_at: str | None = None


@dataclass(frozen=True)
class BuildIdentityMatch:
    state: str
    match_basis: str | None
    matched_registry_sha256: str | None
    matched_registry_slot: str | None
    reason: str | None
    matched_build_id: str | None = None


def subject_build_key(
    bundle_id: str | None,
    short_version: str | None,
    build_version: str | None,
) -> str | None:
    if not bundle_id or not short_version or not build_version:
        return None
    canonical = "\0".join((bundle_id, short_version, build_version)).encode()
    return f"build-v1:{hashlib.sha256(canonical).hexdigest()}"


_INSTALLED_REQUIRED = ("build_id", "artifact_sha256", "short_version", "build_version")
INSTALLED_REF_BASIS = "installed_build_ref_short_build"
INSTALLED_REF_MARKER_BASIS = "installed_build_ref_build_marker"
BUILD_MARKER_FILENAME = "autophone-ui-eval-build-marker.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _match_installed_ref(
    short: str,
    build: str | None,
    installed_ref: Mapping[str, Any],
    *,
    build_marker_match: str | None = None,
) -> BuildIdentityMatch | None:
    """Compare observed device tuple (and optional verified build marker) against installed Build.

    Version-tuple equality alone remains ``ambiguous`` (never ``exact`` / ``confirmed``):
    builds sharing ``short_version`` + ``build_version`` cannot be distinguished without
    build-marker byte equality. Only an explicit verified ``build_marker_match == "exact"``
    promotes the match to ``exact`` with the exact ``artifact_sha256`` and ``build_id``.
    """
    if any(installed_ref.get(key) in (None, "") for key in _INSTALLED_REQUIRED):
        return None
    expected_short = str(installed_ref["short_version"])
    expected_build = str(installed_ref["build_version"])
    if short != expected_short or (build and build != expected_build):
        return BuildIdentityMatch(
            "mismatch", INSTALLED_REF_BASIS, None, None,
            "installed_build_ref_version_mismatch",
        )
    if not build:
        return BuildIdentityMatch(
            "unavailable", INSTALLED_REF_BASIS, None, None, "device_build_version_missing",
        )
    if build_marker_match == "mismatch":
        return BuildIdentityMatch(
            "mismatch", INSTALLED_REF_MARKER_BASIS, None, None,
            "installed_build_marker_mismatch",
        )
    if build_marker_match == "exact":
        artifact_sha = str(installed_ref["artifact_sha256"]).strip().lower()
        build_id = str(installed_ref["build_id"]).strip()
        if not _SHA256_RE.fullmatch(artifact_sha) or not build_id:
            return BuildIdentityMatch(
                "ambiguous", INSTALLED_REF_BASIS, None, None,
                "installed_build_ref_sha_invalid",
            )
        slot_val = installed_ref.get("slot")
        matched_slot = str(slot_val) if slot_val not in (None, "") else None
        return BuildIdentityMatch(
            "exact",
            INSTALLED_REF_MARKER_BASIS,
            artifact_sha,
            matched_slot,
            None,
            matched_build_id=build_id,
        )
    if build_marker_match == "unavailable":
        return BuildIdentityMatch(
            "ambiguous", INSTALLED_REF_BASIS, None, None,
            "device_build_marker_unavailable",
        )
    return BuildIdentityMatch(
        "ambiguous", INSTALLED_REF_BASIS, None, None, "build_marker_not_compared",
    )


def match_registry(
    observation: DeviceBuildObservation,
    submission_ref: SubmissionBuildRef,
    registry_entries: Iterable[dict[str, Any]],
    installed_ref: Mapping[str, Any] | None = None,
    *,
    registry_schema_version: str | None = None,
    build_marker_match: str | None = None,
) -> BuildIdentityMatch:
    """Match the device tuple against the Registry.

    Legacy (v1) manifests keep the default-slot / ``packages`` row logic.  A
    Package Store v2 manifest has no legacy rows. Its default slot remains a
    catalog selection; the frozen installed Build ref is the comparison basis.
    """
    entries = list(registry_entries)
    short = observation.short_version
    build = observation.build_version
    if not short:
        return BuildIdentityMatch("unavailable", None, None, None, "device_short_version_missing")

    def same_available(row: dict[str, Any]) -> bool:
        row_short = row.get("short_version")
        row_build = row.get("build_version")
        if row_short and str(row_short) != short:
            return False
        if build and row_build and str(row_build) != build:
            return False
        # Historical rows with only `version` can support version_only, but do
        # not get promoted into the separated short_version field.
        if not row_short and row.get("version") and str(row.get("version")) != short:
            return False
        return bool(row_short or row.get("version"))

    if (installed_ref and not entries
            and (not submission_ref.default_slot or registry_schema_version == 'v2')):
        installed_match = _match_installed_ref(
            short, build, installed_ref, build_marker_match=build_marker_match,
        )
        if installed_match is not None:
            return installed_match

    candidates = [row for row in entries if same_available(row)]
    if len(candidates) > 1:
        return BuildIdentityMatch("ambiguous", "available_version_fields", None, None, "multiple_registry_matches")

    submission_complete = bool(submission_ref.short_version and submission_ref.build_version)
    device_complete = bool(short and build)
    if submission_complete and device_complete:
        if (
            submission_ref.short_version == short
            and submission_ref.build_version == build
            and submission_ref.default_sha256
        ):
            return BuildIdentityMatch(
                "exact",
                "bundle_short_build_unique_registry_ref",
                submission_ref.default_sha256,
                submission_ref.default_slot,
                None,
            )
        return BuildIdentityMatch("mismatch", "bundle_short_build", None, None, "submission_device_version_mismatch")
    if candidates:
        row = candidates[0]
        return BuildIdentityMatch(
            "version_only",
            "short_version",
            str(row.get("sha256")) if row.get("sha256") else None,
            str(row.get("slot")) if row.get("slot") else None,
            "complete_version_tuple_unavailable",
        )
    if submission_ref.default_version and submission_ref.default_version == short:
        return BuildIdentityMatch(
            "version_only", "default_version", submission_ref.default_sha256,
            submission_ref.default_slot, "registry_version_fields_not_separated",
        )
    return BuildIdentityMatch("mismatch", "short_version", None, None, "no_registry_version_match")


def _running_app(response: dict[str, Any], bundle_id: str) -> dict[str, Any] | None:
    rows = [
        row for row in response.get("apps") or []
        if str(row.get("bundle_id") or "") == bundle_id and int(row.get("pid") or 0) > 0
    ]
    return rows[0] if len(rows) == 1 else None


def _resolve_build_marker_match(
    installed_ref: Mapping[str, Any] | None,
    *,
    live_marker_match: str | None,
    observed_marker_sha256: str | None,
    world_subject: Mapping[str, Any] | None,
) -> str | None:
    """Reconcile live PID-stable build-marker check with frozen world-identity evidence."""
    world_state: str | None = None
    world_marker_sha: str | None = None
    if isinstance(world_subject, Mapping) and isinstance(installed_ref, Mapping):
        world_installed = world_subject.get("installed_build_ref")
        if (
            isinstance(world_installed, Mapping)
            and world_installed.get("build_id") == installed_ref.get("build_id")
            and world_installed.get("artifact_sha256") == installed_ref.get("artifact_sha256")
        ):
            world_state = str(world_subject.get("state") or "") or None
            world_obs = world_subject.get("device_observation")
            if isinstance(world_obs, Mapping):
                raw_sha = world_obs.get("build_marker_sha256")
                if isinstance(raw_sha, str) and _SHA256_RE.fullmatch(raw_sha.strip().lower()):
                    world_marker_sha = raw_sha.strip().lower()
    if world_state == "mismatch" or live_marker_match == "mismatch":
        return "mismatch"
    if live_marker_match == "exact":
        if world_marker_sha is not None and observed_marker_sha256 != world_marker_sha:
            return "mismatch"
        return "exact"
    if live_marker_match == "unavailable":
        return "unavailable"
    return None


def probe(
    *,
    bundle_id: str,
    request_fn: Callable[[dict[str, Any]], Any],
    submission_ref: SubmissionBuildRef,
    registry_entries: Iterable[dict[str, Any]],
    ready_evidence: dict[str, Any] | None = None,
    installed_ref: Mapping[str, Any] | None = None,
    registry_schema_version: str | None = None,
    expected_build_marker: bytes | None = None,
    world_subject: Mapping[str, Any] | None = None,
) -> tuple[DeviceBuildObservation, BuildIdentityMatch, SubjectEpoch]:
    """Probe app_list → Info.plist (+ optional build marker) → app_list and require a stable PID."""
    before_raw = request_fn({"t": "app_list", "filter": "running"})
    before = before_raw[0] if isinstance(before_raw, tuple) else before_raw
    row = _running_app(before, bundle_id)
    if row is None:
        raise RuntimeError(f"running app not uniquely found for {bundle_id}")
    pid = int(row.get("pid") or 0)
    install_path = str(row.get("path") or "")
    if not install_path or "\0" in install_path:
        raise RuntimeError("app_list did not return a valid install path")

    observed_marker_sha256: str | None = None
    live_marker_match: str | None = None
    with tempfile.TemporaryDirectory(prefix="autophone-build-identity-") as tmp:
        local = Path(tmp) / "Info.plist"
        pulled_raw = request_fn({
            "t": "file_get",
            "path": f"{install_path.rstrip('/')}/Info.plist",
            "save": str(local),
        })
        pulled = pulled_raw[0] if isinstance(pulled_raw, tuple) else pulled_raw
        if not pulled.get("ok") or not local.is_file():
            raise RuntimeError(f"Info.plist file_get failed: {pulled}")
        identity = bundle_identity(parse_plist(local.read_bytes()))
        if expected_build_marker is not None:
            local_marker = Path(tmp) / BUILD_MARKER_FILENAME
            try:
                pulled_marker_raw = request_fn({
                    "t": "file_get",
                    "path": f"{install_path.rstrip('/')}/{BUILD_MARKER_FILENAME}",
                    "save": str(local_marker),
                })
                pulled_marker = (
                    pulled_marker_raw[0]
                    if isinstance(pulled_marker_raw, tuple)
                    else pulled_marker_raw
                )
            except Exception:  # noqa: BLE001 - unreadable marker fails closed as unavailable
                pulled_marker = {"ok": False}
            if isinstance(pulled_marker, Mapping) and pulled_marker.get("ok") and local_marker.is_file():
                actual_marker = local_marker.read_bytes()
                observed_marker_sha256 = hashlib.sha256(actual_marker).hexdigest()
                live_marker_match = "exact" if actual_marker == expected_build_marker else "mismatch"
            else:
                live_marker_match = "unavailable"

    after_raw = request_fn({"t": "app_list", "filter": "running"})
    after = after_raw[0] if isinstance(after_raw, tuple) else after_raw
    after_row = _running_app(after, bundle_id)
    after_path = str(after_row.get("path") or "") if after_row is not None else ""
    if (
        after_row is None
        or int(after_row.get("pid") or 0) != pid
        or (after_path and after_path != install_path)
    ):
        raise RuntimeError("app PID changed during build identity probe")
    if identity["bundle_id"] != bundle_id:
        raise RuntimeError("installed Info.plist bundle id does not match profile")

    visible = None if ready_evidence is None else bool(ready_evidence.get("available"))
    evidence = None if ready_evidence is None else str(ready_evidence.get("evidence") or "")
    observed = DeviceBuildObservation(
        bundle_id=bundle_id,
        short_version=identity["short_version"],
        build_version=identity["build_version"],
        install_path=install_path,
        pid=pid,
        observed_at=now_iso(),
        foreground_visible=visible,
        foreground_evidence=evidence or None,
        producer_liveness="confirmed_before_after",
        build_marker_sha256=observed_marker_sha256,
    )
    marker_match = _resolve_build_marker_match(
        installed_ref,
        live_marker_match=live_marker_match,
        observed_marker_sha256=observed_marker_sha256,
        world_subject=world_subject,
    )
    match = match_registry(
        observed,
        submission_ref,
        registry_entries,
        installed_ref,
        registry_schema_version=registry_schema_version,
        build_marker_match=marker_match,
    )
    epoch = SubjectEpoch(
        epoch_ref=f"epoch-{uuid.uuid4().hex}",
        pid=pid,
        bundle_id=bundle_id,
        short_version=observed.short_version,
        build_version=observed.build_version,
        started_at=observed.observed_at,
        source="uds_info_plist",
    )
    return observed, match, epoch


def _state(match: BuildIdentityMatch, key: str | None) -> str:
    if match.state == "mismatch":
        return "mismatch"
    if match.state == "ambiguous":
        return "ambiguous"
    if match.state == "version_only":
        return "version_only"
    if key is None:
        return "unavailable"
    return "confirmed"


def write_probe(
    run_dir: Path,
    *,
    run_id: str,
    app: str,
    observation: DeviceBuildObservation,
    match: BuildIdentityMatch,
    epoch: SubjectEpoch,
    candidate_binding: Mapping[str, Any] | None = None,
) -> Path:
    existing = _load_existing_document(run_dir) or {}
    epochs: list[dict[str, Any]] = []
    live_matches = False
    for raw in existing.get("subject_epochs") or []:
        item = dict(raw)
        is_live = item.get("superseded_at") is None
        same_subject = (
            int(item.get("pid") or 0) == epoch.pid
            and str(item.get("bundle_id") or "") == epoch.bundle_id
            and (item.get("short_version") or None) == epoch.short_version
            and (item.get("build_version") or None) == epoch.build_version
        )
        same_candidate = (
            (not item.get("candidate_identity_ref")
             or (item.get("epoch_ref") == epoch.epoch_ref and item.get("candidate_identity") is not None))
            if candidate_binding is None else all(
                item.get(key) == value for key, value in candidate_binding.items()
            )
        )
        if is_live and same_subject and same_candidate:
            live_matches = True
        elif is_live:
            item["superseded_at"] = observation.observed_at
        epochs.append(item)
    if not live_matches:
        if any(item.get("epoch_ref") == epoch.epoch_ref for item in epochs):
            raise ValueError("candidate_epoch_ref_reused")
        epochs.append({**asdict(epoch), **dict(candidate_binding or {})})
    identity_source = (
        "combined"
        if any(row.get("source") == "frida_bundle" for row in epochs)
        else "uds_info_plist"
    )
    payload = {
        "schema_version": "v1",
        "run_id": run_id,
        "app": app,
        "identity_state": _state(match, observation.subject_build_key),
        "identity_source": identity_source,
        "subject_build_key": observation.subject_build_key,
        "device_observation": asdict(observation),
        "match": asdict(match),
        "subject_epochs": epochs,
        "updated_at": now_iso(),
    }
    if candidate_binding is not None or existing.get("schema_version") == "v2":
        payload["schema_version"] = "v2"
        payload["candidate_identity"] = next(
            (row.get("candidate_identity") for row in epochs if row.get("superseded_at") is None), None
        )
    _validate_document(payload)
    return write_json(run_dir / SIDECAR, payload)


def write_unavailable(run_dir: Path, *, run_id: str, app: str, reason: str) -> Path:
    existing = _load_existing_document(run_dir)
    epochs = list((existing or {}).get("subject_epochs") or [])
    payload = {
        "schema_version": "v1",
        "run_id": run_id,
        "app": app,
        "identity_state": "unavailable",
        "identity_source": "unavailable",
        "subject_build_key": None,
        "device_observation": None,
        "match": {"state": "unavailable", "reason": reason},
        "subject_epochs": epochs,
        "updated_at": now_iso(),
    }
    if (existing or {}).get("schema_version") == "v2":
        payload["schema_version"] = "v2"
        payload["candidate_identity"] = existing["candidate_identity"]
    _validate_document(payload)
    return write_json(run_dir / SIDECAR, payload)


_LABEL_BUILD = re.compile(r"^(?P<short>.+)-(?P<build>bld-[0-9a-f]{12,64})$")


def label_build_mismatch(
    build_label: str | None, package: Mapping[str, Any] | None,
) -> str | None:
    """Why a Run's compared Build is not the Build a ``build_label`` names.

    Labels shaped ``<short_version>-<build_id prefix>`` (for example
    ``1.0.0-bld-b1cc762bdd26``) name one Build; the Run's
    ``comparison_build_id`` must start with that prefix and its Build's
    short version must equal the label's.  A label that contains ``bld-`` but
    does not parse returns ``build_label_build_unparsable``; other label shapes
    name no Build and return None.
    """
    parsed = _LABEL_BUILD.match(build_label or "")
    if parsed is None:
        # A label that mentions a build id but does not parse (upper case,
        # prefix shorter than 12 hex digits) must not pass as "names no Build".
        if "bld-" in (build_label or "").lower():
            return "build_label_build_unparsable"
        return None
    package = package or {}
    build_id = package.get("comparison_build_id")
    if not isinstance(build_id, str) or not build_id:
        return "comparison_build_id_missing"
    if not build_id.startswith(parsed["build"]):
        return "comparison_build_id_not_label_build"
    ref = package.get("registry_build_ref")
    short = ref.get("short_version") if isinstance(ref, Mapping) else None
    if short is not None and str(short) != parsed["short"]:
        return "comparison_short_version_not_label_version"
    return None


def build_identity_unconfirmed(package: Mapping[str, Any] | None) -> str | None:
    """Why a Run's ``package`` does not confirm the installed Build; None when it does.

    Shared by the negative-control probe, the preparation card
    ``runtime_verified`` and the golden-path preflight.  Only fields already
    in the Run result are read:

    - ``build_identity_match`` must be ``match``;
    - ``comparison_granularity=build_marker`` is accepted for every Build;
    - ``comparison_granularity=info_plist`` (BL-241) is accepted only when the
      compared Build (``registry_build_ref.source_revision.kind``) is
      ``unknown``; Builds of other kinds still need the build marker;
    - a missing ``comparison_granularity`` is a result written before BL-190
      and keeps its previous acceptance;
    - any other granularity is not accepted.
    """
    package = package or {}
    if package.get("build_identity_match") != "match":
        return "build_identity_not_matched"
    granularity = package.get("comparison_granularity")
    if granularity in (None, "build_marker"):
        return None
    if granularity == "info_plist":
        ref = package.get("registry_build_ref")
        revision = ref.get("source_revision") if isinstance(ref, Mapping) else None
        kind = revision.get("kind") if isinstance(revision, Mapping) else None
        return None if kind == "unknown" else "info_plist_match_requires_unknown_source_revision"
    return f"build_identity_granularity_not_accepted:{granularity}"


def _validate_document(raw: dict[str, Any]) -> None:
    version = raw.get("schema_version")
    if version not in ("v1", "v2"):
        raise ValueError("build_identity_schema_unsupported")
    validate_or_raise(f"build-identity.{version}.json", raw)
    if version == "v1":
        return
    epochs = raw["subject_epochs"]
    refs = [row["epoch_ref"] for row in epochs]
    live = [row for row in epochs if row["superseded_at"] is None]
    if len(refs) != len(set(refs)) or len(live) > 1:
        raise ValueError("candidate_epoch_ambiguous")
    expected = live[0].get("candidate_identity") if live else None
    if raw["candidate_identity"] != expected:
        raise ValueError("candidate_epoch_identity_mismatch")
    for row in epochs:
        digest = row.get("candidate_identity_sha256")
        historical_host = (row.get("superseded_at") is not None
                           and row.get("candidate_identity") is None
                           and "candidate_observed_at" in row)
        if (digest and not historical_host
                and row["candidate_identity_ref"] != f"candidate-identities/{digest}.json"):
            raise ValueError("candidate_epoch_identity_reference_mismatch")
        identity = row.get("candidate_identity")
        if identity and identity["bundle_id"] != row["bundle_id"]:
            raise ValueError("candidate_epoch_identity_mismatch")


def load_document(run_dir: Path) -> dict[str, Any] | None:
    try:
        raw = read_json(run_dir / SIDECAR)
        if not isinstance(raw, dict):
            return None
        _validate_document(raw)
        return raw
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _load_existing_document(run_dir: Path) -> dict[str, Any] | None:
    doc = load_document(run_dir)
    path = run_dir / SIDECAR
    if run_dir.is_symlink() or path.is_symlink() or (path.exists() and doc is None):
        raise ValueError("build_identity_document_invalid")
    return doc


def device_observation(run_dir: Path) -> dict[str, Any] | None:
    """The UDS Info.plist observation recorded for this run, if any."""
    doc = load_document(run_dir) or {}
    value = doc.get("device_observation")
    return dict(value) if isinstance(value, dict) and value else None


def device_short_version(run_dir: Path) -> str | None:
    value = (device_observation(run_dir) or {}).get("short_version")
    return str(value) if value else None


def record_frida_epoch(
    run_dir: Path,
    *,
    run_id: str,
    app: str,
    pid: int,
    build_info: dict[str, Any],
) -> SubjectEpoch:
    """Create a new epoch and supersede the previous live epoch atomically."""
    doc = _load_existing_document(run_dir) or {
        "schema_version": "v1",
        "run_id": run_id,
        "app": app,
        "identity_state": "unavailable",
        "identity_source": "frida_bundle",
        "subject_build_key": None,
        "device_observation": None,
        "match": {"state": "unavailable", "reason": "uds_probe_not_recorded"},
        "subject_epochs": [],
    }
    # A capture of the same process can reuse the independently verified
    # candidate epoch. Frida's version tuple cannot establish a new candidate.
    if (doc.get("schema_version") == "v2" and doc.get("candidate_identity")
            and doc.get("run_id") == run_id and doc.get("app") == app
            and doc.get("identity_state") not in {"unavailable", "mismatch"}):
        live = [row for row in doc.get("subject_epochs", [])
                if row.get("superseded_at") is None]
        if len(live) == 1:
            row = live[0]
            if (row.get("candidate_identity") == doc["candidate_identity"]
                    and type(pid) is int and row.get("pid") == pid
                    and row.get("bundle_id") == build_info.get("bundle_id")
                    and row.get("short_version") == (build_info.get("short_version") or None)
                    and row.get("build_version") == (build_info.get("build_version") or None)):
                return SubjectEpoch(**{key: row[key] for key in SubjectEpoch.__dataclass_fields__
                                       if key in row})
    timestamp = now_iso()
    epochs = []
    for raw in doc.get("subject_epochs") or []:
        item = dict(raw)
        if item.get("superseded_at") is None:
            item["superseded_at"] = timestamp
        epochs.append(item)
    epoch = SubjectEpoch(
        epoch_ref=f"epoch-{uuid.uuid4().hex}",
        pid=int(pid),
        bundle_id=str(build_info.get("bundle_id") or ""),
        short_version=(str(build_info["short_version"]) if build_info.get("short_version") else None),
        build_version=(str(build_info["build_version"]) if build_info.get("build_version") else None),
        started_at=timestamp,
        source="frida_bundle",
    )
    epochs.append(asdict(epoch))
    key = subject_build_key(epoch.bundle_id, epoch.short_version, epoch.build_version)
    device = doc.get("device_observation") or {}
    uds_tuple = (
        str(device.get("bundle_id") or ""),
        str(device.get("short_version") or ""),
        str(device.get("build_version") or ""),
    )
    frida_tuple = (
        epoch.bundle_id,
        epoch.short_version or "",
        epoch.build_version or "",
    )
    if device and uds_tuple != frida_tuple:
        identity_state = "mismatch"
        match = {
            "state": "mismatch",
            "match_basis": "uds_frida_bundle_tuple",
            "matched_registry_sha256": None,
            "matched_registry_slot": None,
            "reason": "frida_uds_build_identity_mismatch",
        }
    elif device:
        identity_state = str(doc.get("identity_state") or "unavailable")
        match = doc.get("match") or {}
    else:
        # A Frida process tuple proves the observed process identity, but without
        # the UDS/Registry match it cannot prove a submission Registry ref.
        identity_state = "version_only" if key else "unavailable"
        match = {
            "state": "version_only" if key else "unavailable",
            "match_basis": "frida_bundle_tuple" if key else None,
            "matched_registry_sha256": None,
            "matched_registry_slot": None,
            "reason": "uds_registry_match_unavailable",
        }
    doc.update({
        "identity_state": identity_state,
        "identity_source": "combined" if doc.get("device_observation") else "frida_bundle",
        "subject_build_key": key or doc.get("subject_build_key"),
        "match": match,
        "subject_epochs": epochs,
        "updated_at": timestamp,
    })
    if doc.get("schema_version") == "v2":
        doc["candidate_identity"] = None
    _validate_document(doc)
    write_json(run_dir / SIDECAR, doc)
    return epoch


def record_candidate_epoch(
    run_dir: Path, *, run_id: str, app: str, pid: int, bundle_id: str,
    candidate_identity_sha256: str, candidate_identity_ref: str, observed_at: str,
    candidate_changed: bool,
    candidate_identity: Mapping[str, Any] | None = None,
) -> SubjectEpoch:
    """Persist typed host-verified identity without changing Registry facts.

    Legacy untyped bindings are read-only; callers must supply validated identity.
    """
    if candidate_identity is None:
        raise ValueError("candidate_identity_required")
    doc = _load_existing_document(run_dir)
    if doc is None:
        if (run_dir / SIDECAR).exists():
            raise ValueError("build_identity_unreadable")
        doc = {
            "schema_version": "v1", "run_id": run_id, "app": app,
            "identity_state": "unavailable", "identity_source": "unavailable",
            "subject_build_key": None, "device_observation": None,
            "match": {"state": "unavailable", "reason": "uds_probe_not_recorded"},
            "subject_epochs": [],
        }
    if doc["run_id"] != run_id or doc["app"] != app:
        raise ValueError("build_identity_run_mismatch")
    epochs = [dict(row) for row in doc["subject_epochs"]]
    live = [row for row in epochs if row.get("superseded_at") is None]
    previous = live[-1] if live else {}
    if (doc.get("schema_version") == "v2" and not candidate_changed
            and len(live) == 1 and previous.get("pid") == pid
            and previous.get("bundle_id") == bundle_id
            and previous.get("candidate_identity") == dict(candidate_identity)):
        return SubjectEpoch(**{name: previous[name] for name in SubjectEpoch.__dataclass_fields__
                               if name in previous})
    timestamp = now_iso()
    for row in live:
        row["superseded_at"] = timestamp
    epoch = SubjectEpoch(
        epoch_ref=f"epoch-{uuid.uuid4().hex}", pid=pid, bundle_id=bundle_id,
        short_version=previous.get("short_version") if previous.get("bundle_id") == bundle_id else None,
        build_version=previous.get("build_version") if previous.get("bundle_id") == bundle_id else None,
        started_at=timestamp, source="rn_candidate",
    )
    epochs.append({**asdict(epoch), "candidate_identity_sha256": candidate_identity_sha256,
                   "candidate_identity_ref": candidate_identity_ref,
                   "candidate_identity": dict(candidate_identity)})
    doc.update(schema_version="v2", candidate_identity=dict(candidate_identity),
               subject_epochs=epochs, updated_at=timestamp)
    _validate_document(doc)
    write_json(run_dir / SIDECAR, doc)
    return epoch


def rpc_build_info(exports: Any) -> dict[str, Any]:
    fn = getattr(exports, "app_build_info", None) or getattr(exports, "appBuildInfo", None)
    if fn is None:
        raise RuntimeError("capture agent does not expose appBuildInfo")
    value = fn()
    if not isinstance(value, dict):
        raise RuntimeError("appBuildInfo returned a non-object")
    return value
