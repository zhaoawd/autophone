"""Host-managed IPA Asset catalog and VM installation adapter.

The store lives on the host under ``packages/<app>/`` and is the single source
of truth: each build is a file with a zero-padded ``slot`` prefix (the order
key) plus a ``manifest.json`` entry. ``default_slot`` is only the selection
used when a caller omits a slot. Per-Rig installed state belongs to
``RigObservedState`` and is not written into this Asset.

Install pipeline (only runs when explicitly invoked):

  1. resolve a slot (explicit / default / latest)
  2. ``mkdir -p`` a guest-writable incoming dir (root shell)
  3. transfer the IPA over the selected upstream UDS or legacy SSH channel
  4. ``ipa_install`` via vphoned UDS (vphone-cli's built-in installer)
     - extraction with libarchive
     - ldid signing with injected entitlements
     - container creation via MCMAppContainer
     - LaunchServices registration via LSApplicationWorkspace
  5. fail-closed on ok=false or LaunchServices verification failure
  6. clean up the incoming copy and return the installed Build identity

Transfer and guest-shell are injectable seams (``transfer=`` / ``shell=``) so
the store/manifest logic is testable on the host with no VM.
"""
from __future__ import annotations

from autophone.device.session import DeviceProtocolError
from autophone.device.ssh_auth import guest_ssh_password, run_password_command
from autophone.device.transfer import GuestTransfer, run_with_auth_retry, upstream_transfer
from autophone.device.stage_guard import OperationClass, guarded_device_operation

import fcntl
import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

from autophone.paths import APP_ASSET_ROOT, STATE_ROOT
from autophone.contracts import validate_or_raise, write_json
from autophone.assets.plist import bundle_identity, parse_plist
from autophone.packages.artifact_store import (
    ArtifactStoreError,
    resolve_artifact_store,
)
from autophone.timing import (
    IterationTimingRecorder,
    STAGE_IPA_INSTALL,
    STAGE_PACKAGE_STORE_ADD,
    STAGE_PACKAGE_STORE_HASH,
    STAGE_SCP,
    STAGE_UICACHE,
    measure_stage,
)
from autophone.packages.model import (
    BuildRecord,
    BuildSelector,
    PackageManifest,
    PackageModelError,
    RegistryBuildRef,
    SourceRevision,
    VersionSlot,
    compute_build_id,
)
from autophone.util import now_iso, read_json

PACKAGES_DIRNAME = "packages"
ROOT = STATE_ROOT
INSTALL_RECEIPT_NAME = "install-receipt.json"
# Target-level receipts (v2): one file per canonical VM target, so several
# rigs can each record what the platform last verifiably installed on them.
INSTALLATIONS_DIRNAME = "installations"
# v1 → v2 migration receipts: one immutable file per applied migration, so an
# adjudication that suppressed a source-revision conflict stays traceable in
# the store itself rather than only in the command line that ran it.
MIGRATIONS_DIRNAME = "migrations"
# Content-addressed IPA bytes for v2 stores.  Migration deliberately does
# not move existing files, so both layouts coexist.
BLOBS_DIRNAME = "blobs"
INSTALL_TARGET_KEYS = ("device", "vm", "vm_dir", "socket")
_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PACKAGE_LOCK_STATE = threading.local()
# Browser-uploaded IPAs land here on the host before being ingested into a
# slot. The store is host-only and add_package reads from a host path, so the
# web file-picker has to drop the bytes somewhere reachable first.
UPLOAD_STAGING_DIRNAME = "_incoming"
TROLLSTORE_HELPER = "/var/jb/Applications/TrollStoreLite.app/trollstorehelper"
GUEST_INCOMING = "/var/mobile/Media/autophone/incoming"

# SSH-over-iproxy defaults (mobile@2222).
SCP_HOST = "127.0.0.1"
SCP_PORT = 2222
SCP_USER = "mobile"

# trollstorehelper logs prose, not exit-coded errors, on some failures, so we
# also scan its output for these markers and fail closed.
_INSTALL_ERROR_MARKERS = ("error", "failed", "failure", "denied", "invalid")
_NATIVE_INSTALL_SUCCESS = re.compile(
    r"Installed via built-in installer as (?:User|System): "
    r"[^()\r\n]+\.app \((?P<bundle_id>[A-Za-z0-9][A-Za-z0-9.-]*)\)\Z",
)


class PackageError(RuntimeError):
    """Store/manifest level problem (unknown app, bad slot, empty store...)."""


class TransferError(DeviceProtocolError):
    """Host→guest copy failed.

    异常契约（BL-129）：宿主→guest 传包有两条实现——包仓库直连的
    ``scp_to_guest`` 与设备会话的
    ``autophone.device.session._ssh_stream_transfer``。两者的失败统一属于
    ``DeviceProtocolError`` 家族：``_ssh_stream_transfer`` 直接抛
    ``DeviceProtocolError``，``scp_to_guest`` 抛本类（``DeviceProtocolError``
    的子类）。调用方按 ``DeviceProtocolError`` 捕获即可覆盖两条实现，按
    ``TransferError`` 捕获则只覆盖包仓库直连这一条。现有
    ``except TransferError`` 的行为不变。
    """


class InstallError(RuntimeError):
    """Guest-side install did not verifiably succeed."""


class SpringBoardResidueRefused(InstallError):
    """安装前残留检查拒绝安装（BL-232 阶段 1，I1）。

    ``code`` 为 ``springboard_frida_residue``（检出 Frida agent 残留区域）或
    ``springboard_residue_check_failed``（检查本身不可判定，fail-closed）；``check`` 为
    ``springboard-residue-check.v2.json`` 记录。拒绝发生在传包与安装之前，设备上的 App
    与平台安装回执都不变。
    """

    def __init__(self, check: Mapping[str, Any]) -> None:
        self.check = dict(check)
        self.code = str(self.check.get("code") or "")
        super().__init__(f"{self.code}: {self.check.get('detail') or ''}")


# Seams: a transfer copies a host file to a guest path; a shell runs a guest
# command and returns (exit_code, combined_output); a verify confirms a bundle
# id is registered on the guest after install; an installer drives the guest's
# native installer over a remote IPA path and returns its result dict.
TransferFn = Callable[[Path, str], None]
ShellFn = Callable[[str], "tuple[int, str]"]
VerifyFn = Callable[[str, ShellFn], bool]
InstallerFn = Callable[[str], "dict[str, Any]"]


@dataclass(frozen=True)
class PackageEntry:
    slot: str
    label: str
    file: str
    bundle_id: str | None
    version: str | None
    release_like: bool
    release_evidence: str | None
    sha256: str
    size: int
    added_at: str
    # Transient marker set by add_package when an identical-sha256 build already
    # existed and was returned instead of re-stored. Never persisted (to_dict
    # omits it) and excluded from equality.
    duplicate: bool = field(default=False, compare=False)
    # v2 Build identity.  Present for every entry, including ones projected
    # from a v1 manifest, so callers never have to fall back to slot-only
    # addressing.  Excluded from equality so v1-era fixtures keep comparing.
    build_id: str | None = field(default=None, compare=False)
    build_version: str | None = field(default=None, compare=False)
    source_revision: SourceRevision | None = field(default=None, compare=False)
    artifact_store_id: str = field(default="central", compare=False)
    artifact_availability: str = field(default="present", compare=False)
    retention_class: str = field(default="unspecified", compare=False)
    # Observable facts recorded at ingest (artifact reuse, version regression).
    # Transient like `duplicate`; never persisted.
    notes: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_build(
        cls, build: "BuildRecord", short_version: str | None,
    ) -> "PackageEntry":
        return cls(
            slot=build.slot,
            label=build.label or (short_version or build.build_id),
            file=build.file,
            bundle_id=build.bundle_id,
            version=short_version,
            release_like=build.release_like,
            release_evidence=build.release_evidence,
            sha256=build.sha256,
            size=build.size,
            added_at=build.added_at,
            build_id=build.build_id,
            build_version=build.build_version,
            source_revision=build.source_revision,
            artifact_store_id=build.artifact_store_id,
            artifact_availability=build.artifact_availability,
            retention_class=build.retention_class,
        )

    def to_view(self) -> dict[str, Any]:
        """Rich projection for API/page consumers (superset of to_dict())."""
        revision = self.source_revision or SourceRevision.unknown()
        return {
            **self.to_dict(),
            "build_id": self.build_id,
            "short_version": self.version,
            "build_version": self.build_version,
            "artifact_store_id": self.artifact_store_id,
            "artifact_availability": self.artifact_availability,
            "retention_class": self.retention_class,
            "source_revision": revision.to_dict(),
            "source_identity": revision.identity,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PackageEntry":
        return cls(
            slot=str(raw["slot"]),
            label=str(raw["label"]),
            file=str(raw["file"]),
            bundle_id=raw.get("bundle_id"),
            version=raw.get("version"),
            release_like=bool(raw.get("release_like", False)),
            release_evidence=raw.get("release_evidence"),
            sha256=str(raw["sha256"]),
            size=int(raw["size"]),
            added_at=str(raw["added_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "label": self.label,
            "file": self.file,
            "bundle_id": self.bundle_id,
            "version": self.version,
            "release_like": self.release_like,
            "release_evidence": self.release_evidence,
            "sha256": self.sha256,
            "size": self.size,
            "added_at": self.added_at,
        }


# --------------------------------------------------------------------------- #
# Store / manifest
# --------------------------------------------------------------------------- #

def _checked_app_id(app: str) -> str:
    app = str(app or "")
    if _APP_SLUG.fullmatch(app) is None:
        raise PackageError(
            f"invalid app id {app!r}: must match {_APP_SLUG.pattern}"
        )
    return app


def store_dir(app: str, *, root: Path = ROOT) -> Path:
    app = _checked_app_id(app)
    packages_root = Path(root) / PACKAGES_DIRNAME
    target = packages_root / app
    resolved_root = packages_root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PackageError(
            f"package store for app {app!r} escapes packages root: {resolved_target}"
        ) from exc
    return target


def manifest_path(app: str, *, root: Path = ROOT) -> Path:
    return store_dir(app, root=root) / "manifest.json"


def install_receipt_path(app: str, *, root: Path = ROOT) -> Path:
    return store_dir(app, root=root) / INSTALL_RECEIPT_NAME


def installations_dir(app: str, *, root: Path = ROOT) -> Path:
    return store_dir(app, root=root) / INSTALLATIONS_DIRNAME


def target_receipt_path(
    app: str, target_fingerprint: str, *, root: Path = ROOT,
) -> Path:
    """Per-target receipt file. The fingerprint is already a sha256 digest, so
    it is used verbatim after a strict format check — never as a raw path."""
    digest = str(target_fingerprint or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise PackageError(f"malformed install target fingerprint {target_fingerprint!r}")
    return installations_dir(app, root=root) / f"{digest.replace(':', '-')}.json"


def migrations_dir(app: str, *, root: Path = ROOT) -> Path:
    return store_dir(app, root=root) / MIGRATIONS_DIRNAME


def migration_receipt_path(app: str, migration_id: str, *, root: Path = ROOT) -> Path:
    """Per-migration receipt file. The id is a derived digest, so it is checked
    against its exact shape before being used as a path component."""
    identifier = str(migration_id or "")
    if not re.fullmatch(r"mig-[0-9a-f]{16,64}", identifier):
        raise PackageError(f"malformed migration id {migration_id!r}")
    return migrations_dir(app, root=root) / f"{identifier}.json"


@contextmanager
def _package_mutation_lock(app: str, *, root: Path):
    """Serialize one app's package read-modify-write operations across processes."""
    app = _checked_app_id(app)
    identity = f"{Path(root).resolve(strict=False)}\0{app}".encode("utf-8")
    lock_key = hashlib.sha256(identity).hexdigest()
    held = getattr(_PACKAGE_LOCK_STATE, "held", set())
    if lock_key in held:
        yield
        return
    lock_root = Path(tempfile.gettempdir()) / "autophone-package-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_root / f"{lock_key}.lock"
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        held = {*held, lock_key}
        _PACKAGE_LOCK_STATE.held = held
        try:
            yield
        finally:
            _PACKAGE_LOCK_STATE.held = held - {lock_key}
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _locked_package_mutation(func):
    @wraps(func)
    def wrapped(app, *args, **kwargs):
        with _package_mutation_lock(app, root=Path(kwargs.get("root", ROOT))):
            return func(app, *args, **kwargs)
    return wrapped


@contextmanager
def _install_target_mutation_lock(identity: Mapping[str, Any]):
    """Serialize the transfer/install/verify commit on one canonical VM target."""
    fingerprint = install_target_fingerprint(identity)
    lock_key = "target-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    held = getattr(_PACKAGE_LOCK_STATE, "held", set())
    if lock_key in held:
        yield
        return
    lock_root = Path(tempfile.gettempdir()) / "autophone-package-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_root / f"{lock_key}.lock"
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        _PACKAGE_LOCK_STATE.held = {*held, lock_key}
        try:
            yield
        finally:
            _PACKAGE_LOCK_STATE.held = held
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _locked_install_target(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        identity = resolve_install_target_identity()
        with _install_target_mutation_lock(identity):
            return func(*args, **kwargs)
    return wrapped


def resolve_install_target_identity() -> dict[str, str]:
    """Resolve the canonical VM target used by a package installation.

    ``resolve_target`` owns the environment-selection rules, including
    ``VPHONE_SOCK`` and ``AP_DEVICE`` overrides.  Paths are made absolute before
    hashing so equivalent relative spellings cannot create different receipts.
    """
    from autophone.device.target import resolve_target

    target = resolve_target()
    vm_dir = target.vm_dir.expanduser().resolve(strict=False)
    socket = target.sock.expanduser().resolve(strict=False)
    device = str(target.device).strip()
    if not device:
        raise PackageError("install target device must not be empty")
    return {
        "device": device,
        "vm": vm_dir.name,
        "vm_dir": vm_dir.as_posix(),
        "socket": socket.as_posix(),
    }


def _normalize_install_target_identity(identity: Mapping[str, Any]) -> dict[str, str]:
    values = {key: str(identity.get(key) or "").strip() for key in INSTALL_TARGET_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise PackageError("install target identity is missing: " + ", ".join(missing))
    vm_dir = Path(values["vm_dir"]).expanduser().resolve(strict=False)
    socket = Path(values["socket"]).expanduser().resolve(strict=False)
    if values["vm"] != vm_dir.name:
        raise PackageError(
            f"install target vm={values['vm']!r} does not match vm_dir={vm_dir}"
        )
    return {
        "device": values["device"],
        "vm": values["vm"],
        "vm_dir": vm_dir.as_posix(),
        "socket": socket.as_posix(),
    }


def install_target_fingerprint(identity: Mapping[str, Any] | None = None) -> str:
    """Return ``sha256:...`` over canonical device/vm/vm_dir/socket identity."""
    normalized = _normalize_install_target_identity(
        identity if identity is not None else resolve_install_target_identity()
    )
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def install_receipt_target_is_valid(receipt: Mapping[str, Any]) -> bool:
    """Whether a receipt's target object and fingerprint are self-consistent."""
    target = receipt.get("target")
    if not isinstance(target, Mapping):
        return False
    try:
        return str(receipt.get("target_fingerprint") or "") == install_target_fingerprint(target)
    except PackageError:
        return False


def install_receipt_matches_current_target(receipt: Mapping[str, Any]) -> bool:
    """Whether a self-consistent receipt belongs to the currently selected VM."""
    return bool(
        install_receipt_target_is_valid(receipt)
        and receipt.get("target_fingerprint") == install_target_fingerprint()
    )


def load_install_receipt(app: str, *, root: Path = ROOT) -> dict[str, Any] | None:
    receipt = read_json(install_receipt_path(app, root=root))
    if receipt is None:
        return None
    validate_or_raise("package-install-receipt.v1.json", receipt)
    return receipt


def load_target_receipt(
    app: str, *, target_fingerprint: str | None = None, root: Path = ROOT,
) -> dict[str, Any] | None:
    """Installed Build Receipt for one Target (v2), or None.

    A v1 store may project its legacy single-file receipt without rewriting it.
    A v2 store only accepts its Target-level receipt.
    """
    try:
        fingerprint = target_fingerprint or install_target_fingerprint()
    except Exception:
        return None
    try:
        path = target_receipt_path(app, fingerprint, root=root)
    except PackageError:
        return None
    receipt = read_json(path)
    if receipt is not None:
        validate_or_raise("package-install-receipt.v2.json", receipt)
        try:
            actual_fingerprint = install_target_fingerprint(receipt.get("target") or {})
        except PackageError:
            return None
        if receipt.get("target_fingerprint") != fingerprint or actual_fingerprint != fingerprint:
            return None
        return receipt
    try:
        if load_registry(app, root=root).source_schema_version != "v1":
            return None
    except (PackageError, PackageModelError):
        return None
    legacy = load_install_receipt(app, root=root)
    if legacy is None:
        return None
    if str(legacy.get("target_fingerprint") or "") != fingerprint:
        return None
    try:
        if install_target_fingerprint(legacy.get("target") or {}) != fingerprint:
            return None
    except PackageError:
        return None
    return _upgrade_legacy_receipt(app, legacy, root=root)


def _upgrade_legacy_receipt(
    app: str, legacy: Mapping[str, Any], *, root: Path = ROOT,
) -> dict[str, Any] | None:
    """Project a v1 receipt onto the v2 shape without writing anything."""
    try:
        registry = load_registry(app, root=root)
        ref = registry.resolve(BuildSelector(
            slot=str(legacy.get("slot") or ""),
            artifact_sha256=str(legacy.get("sha256") or ""),
        ))
    except (PackageError, PackageModelError):
        return None
    return {
        "schema_version": "v2",
        "app": app,
        "build_ref": ref.to_dict(),
        "verified": bool(legacy.get("verified")),
        "installer": str(legacy.get("installer") or "vphone_native"),
        "target": dict(legacy.get("target") or {}),
        "target_fingerprint": str(legacy.get("target_fingerprint") or ""),
        "installed_at": str(legacy.get("installed_at") or ""),
    }


def _active_slot_build_id(
    app: str, registry: PackageManifest, *, root: Path = ROOT,
) -> str | None:
    """v1 spelling of "currently installed": the manifest's default slot.

    Reads the raw document rather than ``load_manifest`` so the pre-split
    ``active_slot`` spelling is still visible; ``default_slot`` is checked first
    because any explicit write since the split persists only that key.
    """
    if registry.source_schema_version != "v1":
        return None
    document = read_json(manifest_path(app, root=root)) or {}
    active = str(document.get("default_slot") or document.get("active_slot") or "")
    return registry.legacy_slot_aliases.get(active) if active else None


def installed_build_id(app: str, *, root: Path = ROOT) -> str | None:
    """Build ID the platform last installed on the current Target.

    Deliberately independent of ``verified``: this answers "which Build is on
    the device", and callers that require verified evidence (the requirement
    gate, retention) read the receipt's ``verified`` flag themselves.
    """
    receipt = load_target_receipt(app, root=root)
    if not receipt:
        return None
    return str((receipt.get("build_ref") or {}).get("build_id") or "") or None


def _write_target_receipt(
    app: str,
    ref: RegistryBuildRef,
    *,
    verified: bool,
    installer: str,
    target: Mapping[str, str],
    target_fingerprint: str,
    root: Path = ROOT,
) -> Path:
    receipt = {
        "schema_version": "v2",
        "app": app,
        "build_ref": ref.to_dict(),
        "verified": bool(verified),
        "installer": installer,
        "target": dict(target),
        "target_fingerprint": target_fingerprint,
        "installed_at": now_iso(),
    }
    validate_or_raise("package-install-receipt.v2.json", receipt)
    return write_json(target_receipt_path(app, target_fingerprint, root=root), receipt)


def _write_install_receipt(
    app: str,
    entry: "PackageEntry",
    *,
    verified: bool,
    installer: str,
    target_fingerprint: str | None = None,
    target_identity: Mapping[str, Any] | None = None,
    root: Path = ROOT,
) -> Path:
    target = _normalize_install_target_identity(
        target_identity if target_identity is not None else resolve_install_target_identity()
    )
    fingerprint = install_target_fingerprint(target)
    if target_fingerprint is not None:
        # Compatibility seam used by mismatch tests.  Production callers omit
        # it; a supplied value deliberately makes the receipt fail the
        # self-consistency helper while remaining schema-valid.
        supplied = str(target_fingerprint).strip()
        if not supplied:
            raise PackageError("install target fingerprint must not be empty")
        fingerprint = (
            supplied if supplied.startswith("sha256:") and len(supplied) == 71
            else "sha256:" + hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        )
    receipt = {
        "schema_version": "v1",
        "app": app,
        "slot": entry.slot,
        "sha256": entry.sha256,
        "verified": bool(verified),
        "installer": installer,
        "target": target,
        "target_fingerprint": fingerprint,
        "installed_at": now_iso(),
    }
    validate_or_raise("package-install-receipt.v1.json", receipt)
    return write_json(install_receipt_path(app, root=root), receipt)


def _v1_write_refused(app: str, operation: str) -> PackageError:
    return PackageError(
        f"app {app!r} uses a legacy package manifest v1, which is read-only; "
        f"run `app package migrate {app} --apply` before {operation}"
    )


def _empty_manifest(app: str, bundle_id: str | None) -> dict[str, Any]:
    """The document for a store that has no manifest on disk yet.

    New stores start at v2: ``_add_package_snapshot`` already ingests into the
    v2 path when no manifest exists (``manifest_schema_version(...) or "v2"``)
    and ``PackageManifest.to_dict()`` only emits v2, so a v1 shape here was
    only ever an in-memory placeholder that never reached disk. Returning v2
    makes the placeholder agree with what the first write produces (BL-124).
    """
    return {
        "schema_version": "v2",
        "app": app,
        "bundle_id": bundle_id,
        "default_slot": None,
        "version_slots": [],
        "builds": [],
    }


def manifest_schema_version(app: str, *, root: Path = ROOT) -> str | None:
    """Schema version of the on-disk manifest, or None when there is none."""
    raw = read_json(manifest_path(app, root=root))
    if raw is None:
        return None
    return str(raw.get("schema_version") or "")


def load_manifest(
    app: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> dict[str, Any]:
    """Read the raw manifest document, or synthesize an empty one if absent.

    Both schema versions are accepted and returned as-is; callers that need one
    shape should use ``load_registry`` (v2 model) rather than reaching into the
    document. A store with no manifest on disk is synthesized as an empty **v2**
    document (``_empty_manifest``, BL-124); an on-disk v1 document is returned
    as v1 and is never rewritten here, so existing evidence keeps its meaning.
    """
    raw = read_json(manifest_path(app, root=root))
    if raw is None:
        bundle_id = _profile_bundle_id(app, root=root, code_root=code_root)
        return _empty_manifest(app, bundle_id)
    # One-way compatibility read for state created before the default/installed
    # split.  The next explicit Asset write persists only ``default_slot``.
    if "default_slot" not in raw and "active_slot" in raw:
        raw = {**raw, "default_slot": raw.get("active_slot")}
        raw.pop("active_slot", None)
    schema = str(raw.get("schema_version") or "v1")
    if schema not in ("v1", "v2"):
        raise PackageError(f"unsupported package manifest schema_version {schema!r}")
    validate_or_raise(f"package-manifest.{schema}.json", raw)
    bundle_id = _profile_bundle_id(app, root=root, code_root=code_root)
    if bundle_id:
        raw["bundle_id"] = bundle_id
    return raw


def load_registry(
    app: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> PackageManifest:
    """The v2 Version Slot / Build Record view of this store.

    A v1 store is *projected*, not rewritten: each historical slot stays one
    Version Slot holding one Build, so every ``slot + sha256`` reference in
    existing evidence still resolves to exactly one Build.
    """
    raw = load_manifest(app, root=root, code_root=code_root)
    try:
        return PackageManifest.from_dict(raw, app=app)
    except PackageModelError as exc:
        raise PackageError(str(exc)) from exc


def save_manifest(app: str, manifest: dict[str, Any], *, root: Path = ROOT) -> Path:
    schema = str(manifest.get("schema_version") or "v1")
    if schema not in ("v1", "v2"):
        raise PackageError(f"unsupported package manifest schema_version {schema!r}")
    if schema == "v1":
        raise _v1_write_refused(app, "writing package metadata")
    validate_or_raise(f"package-manifest.{schema}.json", manifest)
    path = manifest_path(app, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json(path, manifest)


def save_registry(app: str, registry: PackageManifest, *, root: Path = ROOT) -> Path:
    return save_manifest(app, registry.to_dict(), root=root)


def migrate_manifest_versions(app: str, *, root: Path = ROOT) -> dict[str, Any]:
    """Refuse the retired v1 in-place version backfill operation.

    v1 manifests are read-only. The full migration command derives the same
    release metadata while producing a v2 registry and migration receipt.
    """
    manifest = load_manifest(app, root=root)
    if str(manifest.get("schema_version") or "v1") != "v1":
        raise PackageError(
            f"app {app!r} already uses a v2 package store: short/build versions "
            "are recorded at ingest and there is nothing to backfill"
        )
    raise _v1_write_refused(app, "backfilling package versions")


def _profile_bundle_id(
    app: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> str | None:
    """Best-effort bundle id from the app registry; None if no profile."""
    try:
        return _registered_app(app, root=root, code_root=code_root)
    except Exception:
        return None


def _registered_app(
    app: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> str | None:
    """profile bundle id, or None if the app has no registered profile.

    Distinguishes "no profile" from a profile that exists — unlike
    _profile_bundle_id, callers can act on the absence (e.g. add_package
    rejecting an unknown app)."""
    from autophone.assets.apps import load_app_profile

    profile_root = code_root or (APP_ASSET_ROOT if root == STATE_ROOT else root)
    return load_app_profile(app, root=profile_root).bundle_id


def _entries(registry: PackageManifest) -> list[PackageEntry]:
    from autophone.packages.model import sorted_builds

    return [
        PackageEntry.from_build(build, registry.short_version_of(build))
        for build in sorted_builds(registry.builds)
    ]


def list_packages(
    app: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> list[PackageEntry]:
    """Every Build in the store, ordered by Version Slot then ingest time.

    On a v1 store this is exactly one entry per slot, as before.
    """
    return _entries(load_registry(app, root=root, code_root=code_root))


def get_package(
    app: str, slot: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> PackageEntry:
    """Compatibility entry point: a bare slot resolves to that slot's Candidate.

    Automation must not address a Build by slot alone — use ``resolve_build``
    with a Build ID.  This exists for human and legacy call sites, which on a
    v1 store address a slot that holds exactly one Build anyway.
    """
    return _entry_for(app, BuildSelector(slot=str(slot)), root=root, code_root=code_root)


def resolve_slot(
    app: str, slot: str | None, *, root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    """slot → that slot's Candidate; None → ``default_slot``'s Candidate, else
    the newest Version Slot's Candidate.  Never consults what a Rig has
    installed (BL-011：Build Catalog 不读 Rig 状态)."""
    selector = BuildSelector(slot=str(slot)) if slot is not None else None
    return _entry_for(app, selector, root=root, code_root=code_root)


def _entry_for(
    app: str,
    selector: "BuildSelector | None",
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    registry = load_registry(app, root=root, code_root=code_root)
    ref = _resolve_ref(app, registry, selector, root=root)
    build = registry.build(ref.build_id)
    assert build is not None  # resolve() only returns refs for known builds
    return PackageEntry.from_build(build, ref.short_version)


def _resolve_ref(
    app: str,
    registry: PackageManifest,
    selector: "BuildSelector | None",
    *,
    root: Path = ROOT,
) -> RegistryBuildRef:
    """Build Catalog 解析：显式选择器 → ``default_slot``（v1 仓库即 ``active_slot``
    投影）→ 最新 Version Slot。不读安装回执与 Observed State（BL-011）。"""
    try:
        return registry.resolve(selector)
    except PackageModelError as exc:
        raise PackageError(str(exc)) from exc


def _find_by_sha256(
    app: str, sha256: str, *, root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry | None:
    """Existing Build whose stored .ipa has this exact sha256, else None."""
    for entry in list_packages(app, root=root, code_root=code_root):
        if entry.sha256 == sha256:
            return entry
    return None


def _next_slot(manifest: dict[str, Any], width: int = 3) -> str:
    used = [int(p["slot"]) for p in manifest["packages"]]
    nxt = (max(used) + 1) if used else 1
    return str(nxt).zfill(width)


# --------------------------------------------------------------------------- #
# IPA introspection
# --------------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_ipa_identity(ipa_path: Path) -> dict[str, str | None]:
    """Read the complete bundle/version tuple from an IPA Info.plist."""
    if not zipfile.is_zipfile(ipa_path):
        raise PackageError(f"not a valid .ipa (zip) archive: {ipa_path}")
    empty: dict[str, str | None] = {
        "bundle_id": None,
        "short_version": None,
        "build_version": None,
        "version": None,
        "display_name": None,
        "executable": None,
    }
    with zipfile.ZipFile(ipa_path) as zf:
        info_names = [
            n for n in zf.namelist()
            if n.startswith("Payload/")
            and n.count("/") == 2
            and n.endswith(".app/Info.plist")
        ]
        if not info_names:
            return empty
        try:
            plist = parse_plist(zf.read(sorted(info_names, key=len)[0]))
        except Exception:
            return empty
    identity = bundle_identity(plist)
    # `version` keeps the historical either/or spelling for v1 call sites.  The
    # two version fields never fall back onto each other in the v2 model: a
    # Version Slot keyed off a CFBundleVersion would group unrelated releases.
    short_version = identity["short_version"]
    build_version = identity["build_version"]
    display_name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName")
    return {
        **identity,
        "version": short_version or build_version,
        "display_name": str(display_name) if display_name else None,
    }


def read_ipa_info(ipa_path: Path) -> dict[str, str | None]:
    """Pull identity fields from Payload/*.app/Info.plist into a dict with
    ``bundle_id`` / ``version`` / ``display_name`` keys (each None if absent).

    Display name prefers CFBundleDisplayName (the springboard label) and falls
    back to CFBundleName. Validates the file is a zip; a missing/unreadable
    Info.plist yields all-None rather than raising, so callers can degrade."""
    identity = read_ipa_identity(ipa_path)
    return {
        "bundle_id": identity["bundle_id"],
        "version": identity["version"],
        "display_name": identity["display_name"],
    }


def read_ipa_metadata(ipa_path: Path) -> tuple[str | None, str | None]:
    """(bundle_id, version) from the IPA's Info.plist — thin wrapper over
    read_ipa_info kept for callers that only need the install-relevant pair."""
    info = read_ipa_info(ipa_path)
    return (info["bundle_id"], info["version"])


def inspect_release_like(ipa_path: Path) -> tuple[bool, str | None]:
    """Detect an offline React Native bundle inside the application payload.

    An installable IPA that lacks a bundled JS program may still depend on a
    Metro development server.  M1 treats only a packaged ``*.jsbundle`` as
    deterministic release-like evidence; native-only admission remains a
    future package-policy extension.
    """
    if not zipfile.is_zipfile(ipa_path):
        raise PackageError(f"not a valid .ipa (zip) archive: {ipa_path}")
    with zipfile.ZipFile(ipa_path) as zf:
        candidates = sorted(
            name for name in zf.namelist()
            if name.startswith("Payload/")
            and ".app/" in name
            and name.lower().endswith(".jsbundle")
            and not name.endswith("/")
        )
    if not candidates:
        return False, None
    return True, f"embedded_js_bundle:{candidates[0]}"


def _stored_ipa_path(app: str, filename: str, *, root: Path) -> Path:
    base = store_dir(app, root=root).resolve(strict=False)
    target = (base / filename).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise PackageError(f"package file escapes store for app {app!r}: {filename!r}") from exc
    return target


def _stored_artifact_path(
    app: str,
    filename: str,
    *,
    store_id: str | None,
    root: Path,
    code_root: Path | None,
) -> Path:
    try:
        store = resolve_artifact_store(
            app, store_id, state_root=root, code_root=code_root,
        )
        return store.path(filename)
    except (ArtifactStoreError, FileNotFoundError) as exc:
        raise PackageError(str(exc)) from exc


def _stored_build_path(
    app: str, build: BuildRecord, *, root: Path, code_root: Path | None,
) -> Path:
    return _stored_artifact_path(
        app, build.file, store_id=build.artifact_store_id,
        root=root, code_root=code_root,
    )


@_locked_package_mutation
def verify_stored_package(
    app: str,
    slot: str,
    *,
    backfill_release_metadata: bool = True,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    """Revalidate the Candidate Build of one slot. Compatibility wrapper."""
    entry = get_package(app, slot, root=root, code_root=code_root)
    return verify_stored_build(
        app, str(entry.build_id),
        backfill_release_metadata=backfill_release_metadata,
        root=root, code_root=code_root,
    )


@_locked_package_mutation
def verify_stored_build(
    app: str,
    build_id: str,
    *,
    backfill_release_metadata: bool = True,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    """Revalidate one Build's stored IPA against its registered metadata.

    Legacy entries may omit both release fields.  They are backfilled only
    after the retained IPA's bytes match the recorded sha256; an existing but
    incorrect release claim is treated as an integrity failure.
    """
    registry = load_registry(app, root=root, code_root=code_root)
    build = registry.build(build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_id!r}")
    if build.artifact_availability == "pruned":
        raise PackageError(f"artifact for build {build.build_id} was pruned")
    local = _stored_build_path(app, build, root=root, code_root=code_root)
    return _verify_stored_build_file(
        app, build, local,
        backfill_release_metadata=backfill_release_metadata,
        root=root, code_root=code_root,
    )


def _raw_build_entry(
    document: Mapping[str, Any], build: BuildRecord,
) -> dict[str, Any] | None:
    """The mutable on-disk record for one Build, in either manifest schema."""
    if str(document.get("schema_version") or "v1") == "v1":
        return next(
            (item for item in document.get("packages", [])
             if str(item["slot"]) == build.slot),
            None,
        )
    return next(
        (item for item in document.get("builds", [])
         if str(item["build_id"]) == build.build_id),
        None,
    )


def _verify_stored_build_file(
    app: str,
    build: BuildRecord,
    local: Path,
    *,
    backfill_release_metadata: bool,
    root: Path,
    code_root: Path | None = None,
) -> PackageEntry:
    """Validate the exact file that a caller will consume for this Build."""
    document = load_manifest(app, root=root, code_root=code_root)
    raw = _raw_build_entry(document, build)
    if raw is None:
        raise PackageError(f"app {app!r} has no build {build.build_id!r}")
    label = f"{app}/{build.slot}"

    local = Path(local)
    if not local.is_file():
        raise PackageError(f"store file missing for build {build.build_id}: {local}")
    actual_sha256 = _sha256(local)
    if actual_sha256 != build.sha256:
        raise PackageError(
            f"store integrity mismatch for {label}: "
            f"manifest sha256={build.sha256}, actual={actual_sha256}"
        )
    actual_size = local.stat().st_size
    if actual_size != int(build.size):
        raise PackageError(
            f"store size mismatch for {label}: manifest size={build.size}, actual={actual_size}"
        )

    derived_release_like, derived_release_evidence = inspect_release_like(local)
    release_fields_missing = "release_like" not in raw or "release_evidence" not in raw
    if release_fields_missing:
        if not backfill_release_metadata:
            raise PackageError(f"release metadata missing for {label}")
        if str(document.get("schema_version") or "v1") == "v1":
            raise _v1_write_refused(app, "backfilling release metadata")
        raw["release_like"] = derived_release_like
        raw["release_evidence"] = derived_release_evidence
        save_manifest(app, document, root=root)
        build = replace(
            build,
            release_like=derived_release_like,
            release_evidence=derived_release_evidence,
        )
    elif (
        raw["release_like"] != derived_release_like
        or raw["release_evidence"] != derived_release_evidence
    ):
        raise PackageError(
            f"release metadata mismatch for {label}: "
            f"manifest=({raw['release_like']!r}, {raw['release_evidence']!r}), "
            f"actual=({derived_release_like!r}, {derived_release_evidence!r})"
        )
    registry = load_registry(app, root=root, code_root=code_root)
    return PackageEntry.from_build(build, registry.short_version_of(build))


def _transfer_verified_build(
    app: str,
    build: BuildRecord,
    remote: str,
    transfer: TransferFn,
    *,
    root: Path,
    code_root: Path | None = None,
    timing: IterationTimingRecorder | None = None,
) -> PackageEntry:
    """Copy, validate and transfer one immutable private snapshot of a Build."""
    if build.artifact_availability == "pruned":
        raise PackageError(f"artifact for build {build.build_id} was pruned")
    source = _stored_build_path(app, build, root=root, code_root=code_root)
    if not source.is_file():
        raise PackageError(f"store file missing for build {build.build_id}: {source}")
    with tempfile.TemporaryDirectory(prefix="autophone-package-install-") as temp_dir:
        snapshot = Path(temp_dir) / source.name
        shutil.copy2(source, snapshot)
        verified = _verify_stored_build_file(
            app, build, snapshot,
            backfill_release_metadata=True,
            root=root,
            code_root=code_root,
        )
        with measure_stage(timing, STAGE_SCP):
            transfer(snapshot, remote)
    return verified


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of registering one Build."""

    ref: RegistryBuildRef
    entry: PackageEntry
    duplicate: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_ref": self.ref.to_dict(),
            "duplicate": self.duplicate,
            "notes": list(self.notes),
            **self.entry.to_view(),
        }


def register_build(
    app: str,
    ipa_path: str | Path,
    *,
    source_revision: SourceRevision | None = None,
    label: str | None = None,
    force: bool = False,
    artifact_store_id: str | None = None,
    make_candidate: bool = True,
    retention_class: str | None = None,
    timing: IterationTimingRecorder | None = None,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> RegistrationResult:
    """Register one Build of an App version. The Module Interface entry point.

    A new ``CFBundleShortVersionString`` creates the next Version Slot; a
    version already registered gains another immutable Build inside its slot.
    ``source_revision`` defaults to ``unknown``, which managed delivery refuses
    to consume — it is a recorded gap, never a guess.
    """
    entry = add_package(
        app, ipa_path,
        label=label,
        force=force,
        source_revision=source_revision,
        artifact_store_id=artifact_store_id,
        make_candidate=make_candidate,
        retention_class=retention_class,
        timing=timing,
        root=root,
        code_root=code_root,
    )
    registry = load_registry(app, root=root, code_root=code_root)
    build = registry.build(entry.build_id or "")
    if build is None:
        raise PackageError(f"registered build {entry.build_id!r} is not readable back")
    ref = RegistryBuildRef.from_build(
        app, build, registry.short_version_of(build), resolution_source="build_id",
    )
    return RegistrationResult(
        ref=ref, entry=entry, duplicate=entry.duplicate,
        notes=entry.notes,
    )


def resolve_build(
    app: str,
    selector: BuildSelector | None = None,
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> RegistryBuildRef:
    """Resolve a caller selection to exactly one RegistryBuildRef, fail-closed."""
    registry = load_registry(app, root=root, code_root=code_root)
    return _resolve_ref(app, registry, selector, root=root)


@_locked_package_mutation
def add_package(
    app: str,
    ipa_path: str | Path,
    *,
    label: str | None = None,
    force: bool = False,
    source_revision: SourceRevision | None = None,
    artifact_store_id: str | None = None,
    make_candidate: bool = True,
    retention_class: str | None = None,
    timing: IterationTimingRecorder | None = None,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    """Ingest an IPA into the store: assign the next slot, copy it in, record
    sha256/size and (best-effort) bundle id + version, update the manifest.

    Fail-closed binding (bypass with force=True): the app must have a profile
    under apps/<app>/, and the IPA's parsed bundle id must match that profile's
    fixed bundle id. Different bundles should be registered as different apps.

    Content dedup: the IPA is hashed up front and matched against existing
    slots. A byte-identical build is never re-stored — the existing entry is
    returned with ``duplicate=True`` (one build = one stored .ipa).
    """
    with measure_stage(timing, STAGE_PACKAGE_STORE_ADD):
        src = Path(ipa_path).expanduser()
        if not src.is_file():
            raise PackageError(f"ipa not found: {src}")
        # External upload/build paths remain mutable.  All metadata, release
        # evidence, digest and stored bytes must therefore come from one private
        # snapshot rather than from several reads of the caller-owned path.
        with tempfile.TemporaryDirectory(prefix="autophone-package-add-") as temp_dir:
            snapshot = Path(temp_dir) / "source.ipa"
            shutil.copy2(src, snapshot)
            return _add_package_snapshot(
                app,
                src=src,
                snapshot=snapshot,
                label=label,
                force=force,
                source_revision=source_revision,
                artifact_store_id=artifact_store_id,
                make_candidate=make_candidate,
                retention_class=retention_class,
                timing=timing,
                root=root,
                code_root=code_root,
            )


def _add_package_snapshot(
    app: str,
    *,
    src: Path,
    snapshot: Path,
    label: str | None,
    force: bool,
    source_revision: SourceRevision | None = None,
    artifact_store_id: str | None = None,
    make_candidate: bool = True,
    retention_class: str | None = None,
    timing: IterationTimingRecorder | None = None,
    root: Path,
    code_root: Path | None,
) -> PackageEntry:
    """Ingest metadata and bytes from the same private source snapshot.

    Stores at v2 (which every new store starts at) allocate slots per App
    version and record a Build inside them. Historical v1 stores are readable
    but must be migrated explicitly before registration.
    """
    info = read_ipa_identity(snapshot)
    bundle_id, version = info["bundle_id"], info["version"]
    release_like, release_evidence = inspect_release_like(snapshot)
    with measure_stage(timing, STAGE_PACKAGE_STORE_HASH):
        digest = _sha256(snapshot)
    revision = source_revision or SourceRevision.unknown()
    schema = manifest_schema_version(app, root=root) or "v2"

    if retention_class is not None and retention_class not in {
        "iteration", "candidate", "accepted", "baseline",
    }:
        raise PackageError(f"unknown retention class {retention_class!r}")
    if not make_candidate and retention_class == "candidate":
        raise PackageError("retention class 'candidate' requires Candidate promotion")

    if schema == "v1":
        raise _v1_write_refused(app, "registering a package build")
    return _add_package_snapshot_v2(
        app, src=src, snapshot=snapshot, label=label, force=force,
        info=info, revision=revision,
        release_like=release_like, release_evidence=release_evidence,
        digest=digest, root=root, code_root=code_root,
        artifact_store_id=artifact_store_id,
        make_candidate=make_candidate,
        retention_class=retention_class,
    )


def _checked_ingest_bundle_id(
    app: str, bundle_id: str | None, *, force: bool, root: Path, code_root: Path | None,
) -> str | None:
    """Fail-closed app/bundle binding shared by both ingest paths."""
    profile_bid: str | None = None
    try:
        profile_bid = _registered_app(app, root=root, code_root=code_root)
    except FileNotFoundError:
        if not force:
            raise PackageError(
                f"unknown app {app!r}: no apps/{app}/profile.yaml. "
                f"Fix the name or pass force=True to import anyway.")
    if not force and profile_bid and bundle_id and bundle_id != profile_bid:
        raise PackageError(
            f"bundle id mismatch: IPA is {bundle_id!r} but app {app!r} "
            f"expects {profile_bid!r}. Register a separate app profile for this bundle "
            f"or pass force=True to import anyway.")
    return profile_bid


def _store_ipa_bytes(
    app: str, snapshot: Path, dest_name: str, *,
    digest: str, bundle_id: str | None, version: str | None,
    release_like: bool, release_evidence: str | None, root: Path,
    store_id: str | None = "central", code_root: Path | None = None,
) -> Path:
    """Copy the private snapshot into the store and re-verify what landed."""
    dest = _stored_artifact_path(
        app, dest_name, store_id=store_id, root=root, code_root=code_root,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot, dest)
    if (
        _sha256(dest) != digest
        or read_ipa_metadata(dest) != (bundle_id, version)
        or inspect_release_like(dest) != (release_like, release_evidence)
    ):
        dest.unlink(missing_ok=True)
        raise PackageError("package snapshot changed while it was being ingested; retry the add")
    return dest


def _add_package_snapshot_v2(
    app: str,
    *,
    src: Path,
    snapshot: Path,
    label: str | None,
    force: bool,
    info: Mapping[str, str | None],
    revision: SourceRevision,
    release_like: bool,
    release_evidence: str | None,
    digest: str,
    root: Path,
    code_root: Path | None,
    artifact_store_id: str | None,
    make_candidate: bool,
    retention_class: str | None,
) -> PackageEntry:
    bundle_id = info["bundle_id"]
    version = info["version"]
    short_version = info["short_version"]
    build_version = info["build_version"]
    registry = load_registry(app, root=root, code_root=code_root)
    notes: list[str] = []
    explicit_store = (
        resolve_artifact_store(
            app, artifact_store_id, state_root=root, code_root=code_root,
        )
        if artifact_store_id is not None
        else None
    )

    # §7.3 — identity is (source revision, artifact), not artifact alone.
    matching_bytes = [b for b in registry.builds if b.sha256 == digest]
    same_bytes = [
        b for b in matching_bytes if b.artifact_availability == "present"
    ]
    identical = next((b for b in matching_bytes if b.source_revision == revision), None)

    def reject_explicit_store_conflict(build: BuildRecord) -> None:
        if explicit_store is None or explicit_store.store_id == build.artifact_store_id:
            return
        raise PackageError(
            "explicit artifact store conflict: "
            f"build {build.build_id} reuses Blob {digest} from "
            f"{build.artifact_store_id!r}, but {explicit_store.store_id!r} was requested; "
            f"relocate build {build.build_id} first or omit --artifact-store"
        )

    if identical is not None:
        reject_explicit_store_conflict(identical)
        if identical.artifact_availability == "pruned":
            stored_path = _stored_build_path(
                app, identical, root=root, code_root=code_root,
            )
            created = not stored_path.is_file()
            if created:
                _store_ipa_bytes(
                    app, snapshot, identical.file,
                    digest=digest, bundle_id=bundle_id, version=version,
                    release_like=release_like, release_evidence=release_evidence,
                    root=root, store_id=identical.artifact_store_id,
                    code_root=code_root,
                )
            try:
                verified = _verify_stored_build_file(
                    app, identical, stored_path,
                    backfill_release_metadata=True,
                    root=root, code_root=code_root,
                )
                if (verified.release_like, verified.release_evidence) != (
                    release_like, release_evidence,
                ):
                    raise PackageError(
                        "release metadata derived from restored source does not match "
                        f"stored build {identical.build_id}"
                    )
                current_registry = load_registry(
                    app, root=root, code_root=code_root,
                )
                restored_registry = current_registry.with_artifact(
                    identical.build_id, availability="present",
                )
                save_registry(app, restored_registry, root=root)
            except Exception:
                if created:
                    stored_path.unlink(missing_ok=True)
                raise
            cleanup_staged_upload(src, root=root)
            restored = restored_registry.build(identical.build_id)
            if restored is None:
                raise PackageError(
                    f"restored build {identical.build_id!r} is not readable back"
                )
            return replace(
                PackageEntry.from_build(
                    restored, restored_registry.short_version_of(restored),
                ),
                notes=("artifact_restored",),
            )
        stored = verify_stored_build(
            app, identical.build_id, root=root, code_root=code_root,
        )
        if (stored.release_like, stored.release_evidence) != (
            release_like, release_evidence,
        ):
            raise PackageError(
                "release metadata derived from duplicate source does not match "
                f"stored build {identical.build_id}"
            )
        cleanup_staged_upload(src, root=root)
        return replace(stored, duplicate=True)
    selected_store = explicit_store or resolve_artifact_store(
        app, None, state_root=root, code_root=code_root,
    )
    if same_bytes:
        notes.append("artifact_reused_by_new_source_revision")
    if revision.is_managed and any(
        b.source_revision == revision and b.sha256 != digest for b in registry.builds
    ):
        # An observable fact, not a verdict: why the bytes differ needs its own
        # investigation and must not be inferred from the digests alone.
        notes.append("source_revision_has_multiple_artifacts")

    profile_bid = _checked_ingest_bundle_id(
        app, bundle_id, force=force, root=root, code_root=code_root,
    )

    version_slot = registry.slot_for_version(short_version)
    created_slot: VersionSlot | None = None
    if version_slot is None:
        if not str(short_version or "").strip() and not force:
            raise PackageError(
                "IPA has no CFBundleShortVersionString, so it cannot open a new version "
                "slot. Pass force=True to import it into an 'unknown version' slot."
            )
        slot = registry.next_slot()
        created_slot = VersionSlot(
            slot=slot,
            short_version=str(short_version).strip() if short_version else None,
            candidate_build_id=None,
            created_at=now_iso(),
        )
    else:
        slot = version_slot.slot
        latest = registry.latest_slot
        if latest is not None and slot < latest.slot:
            notes.append("version_regression")

    build_id = compute_build_id(
        app=app, slot=slot, short_version=short_version,
        build_version=build_version, source_revision=revision,
        artifact_sha256=digest,
    )
    if registry.build(build_id) is not None:
        raise PackageError(
            f"build id collision for {app}/{slot}: {build_id} already registered"
        )

    if same_bytes:
        # Same bytes, new Build Record: reuse the Blob rather than storing a
        # second copy (invariant 4/5).
        reused = same_bytes[0]
        if explicit_store is not None:
            matching_store = next(
                (
                    build for build in same_bytes
                    if build.artifact_store_id == explicit_store.store_id
                ),
                None,
            )
            if matching_store is None:
                reject_explicit_store_conflict(reused)
            else:
                reused = matching_store
        dest_name = reused.file
        size = reused.size
        selected_store_id = reused.artifact_store_id
    else:
        selected_store_id = selected_store.store_id
        dest_name = (
            f"{BLOBS_DIRNAME}/{digest}.ipa"
            if selected_store_id == "central"
            else f"{BLOBS_DIRNAME}/sha256/{digest}.ipa"
        )
        dest = _store_ipa_bytes(
            app, snapshot, dest_name, digest=digest, bundle_id=bundle_id, version=version,
            release_like=release_like, release_evidence=release_evidence, root=root,
            store_id=selected_store_id, code_root=code_root,
        )
        size = dest.stat().st_size

    build = BuildRecord(
        build_id=build_id,
        slot=slot,
        label=label or version or src.stem,
        bundle_id=bundle_id,
        build_version=build_version,
        source_revision=revision,
        file=dest_name,
        sha256=digest,
        size=size,
        release_like=release_like,
        release_evidence=release_evidence,
        added_at=now_iso(),
        artifact_store_id=selected_store_id,
        retention_class=retention_class or ("candidate" if make_candidate else "iteration"),
    )
    registry = registry.with_build(
        build, slot=created_slot, make_candidate=make_candidate,
    )
    if profile_bid:
        registry = replace(registry, bundle_id=profile_bid)
    elif not registry.bundle_id and bundle_id:
        registry = replace(registry, bundle_id=bundle_id)
    save_registry(app, registry, root=root)
    cleanup_staged_upload(src, root=root)
    return replace(
        PackageEntry.from_build(build, registry.short_version_of(build)),
        notes=tuple(notes),
    )


def _slugify(text: str) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "-" for c in text.strip()]
    slug = "".join(keep).strip("-")
    return slug or "build"


def upload_staging_dir(*, root: Path = ROOT) -> Path:
    return root / PACKAGES_DIRNAME / UPLOAD_STAGING_DIRNAME


def _upload_filename(filename: str) -> str:
    name = Path(filename or "").name.strip()
    if not name:
        raise PackageError("upload is missing a filename")
    if not name.lower().endswith(".ipa"):
        raise PackageError(f"expected a .ipa file, got {name!r}")
    return name


def staged_upload_path(filename: str, *, root: Path = ROOT) -> Path:
    """Return a non-clobbering path under the upload staging directory."""
    name = _upload_filename(filename)
    staging = upload_staging_dir(root=root)
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / name
    if dest.exists():
        dest = staging / f"{Path(name).stem}-{uuid.uuid4().hex[:8]}.ipa"
    return dest


def is_staged_upload(path: str | Path, *, root: Path = ROOT) -> bool:
    try:
        Path(path).resolve().relative_to(upload_staging_dir(root=root).resolve())
        return True
    except ValueError:
        return False


def cleanup_staged_upload(path: str | Path, *, root: Path = ROOT) -> None:
    target = Path(path)
    if is_staged_upload(target, root=root):
        target.unlink(missing_ok=True)


def stage_uploaded_ipa(
    filename: str, data: bytes, *, root: Path = ROOT
) -> Path:
    """Persist browser-uploaded IPA bytes to the host staging dir, returning the
    saved path. The web file-picker calls this so add_package — which ingests
    from a host path — has something on the host to read. Fail-closed: the name
    must be a ``.ipa`` and the payload non-empty. The filename is reduced to its
    basename so a crafted ``../`` cannot escape the staging dir.
    """
    name = _upload_filename(filename)
    if not data:
        raise PackageError("uploaded file is empty")

    dest = staged_upload_path(name, root=root)
    dest.write_bytes(data)
    return dest


# --------------------------------------------------------------------------- #
# transfer + install
# --------------------------------------------------------------------------- #

@guarded_device_operation(OperationClass.TRANSFER, identity=lambda a: {
    "ssh_endpoint": (a["host"], a["port"]),
})
def scp_to_guest(
    local: Path,
    remote: str,
    *,
    host: str = SCP_HOST,
    port: int = SCP_PORT,
    user: str = SCP_USER,
    password: str | None = None,
    timeout_s: float = 600.0,
) -> None:
    """Stream a host file to the minimal guest over the iproxy SSH tunnel.

    guest 认证被拒时按 ``run_with_auth_retry`` 间隔重试，每次重试从文件开头重新发送；
    其他失败不重试。"""
    try:
        password = guest_ssh_password(password)
    except ValueError as exc:
        raise TransferError(str(exc)) from exc
    cmd = [
        # The native dropbear has neither an SFTP server nor a remote scp
        # binary.  A non-interactive stdin stream works with both dropbear and
        # OpenSSH and avoids either optional subsystem.
        "ssh", "-T", "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        # Force password-only auth: otherwise ssh offers ssh-agent / default
        # identity keys first and the guest hits MaxAuthTries ("Too many
        # authentication failures") before password input.
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "IdentitiesOnly=yes",
        "-o", "NumberOfPasswordPrompts=1",
        f"{user}@{host}",
    ]
    logger.info(f"Starting SSH stream: {local.name} → {user}@{host}:{remote} (port={port}, timeout={timeout_s}s)")

    def attempt() -> subprocess.CompletedProcess:
        with local.open("rb") as source:
            return run_password_command(
                cmd, password=password, stdin=source, capture_output=True, text=True,
                timeout=timeout_s,
            )

    try:
        transfer = GuestTransfer(remote, local.stat().st_size)
        cmd.append(transfer.stream_command("/iosbinpack64/bin/cat"))
        proc = run_with_auth_retry(attempt, what=f"SSH stream {local.name} to {host}:{port}")
    except FileNotFoundError as exc:
        logger.error(f"ssh or source file not available: {exc}")
        raise TransferError(f"ssh or source file not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        try:
            cleanup = run_password_command(
                [*cmd[:-1], transfer.cleanup_command()], password=password, capture_output=True,
                text=True, timeout=min(timeout_s, 15.0),
            )
            if cleanup.returncode != 0:
                logger.warning("Guest transfer cleanup failed: %s", cleanup.stderr.strip()[:300])
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Guest transfer cleanup unavailable")
        logger.error(f"SSH stream timed out after {timeout_s}s")
        raise TransferError(f"SSH stream timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        logger.error(f"SSH stream failed: returncode={proc.returncode}, stderr={proc.stderr.strip()[:300]!r}")
        raise TransferError(
            f"SSH stream exit {proc.returncode}: {proc.stderr.strip()[:300]!r}")
    logger.info(f"SSH stream completed successfully: {local.name} transferred")


SPRINGBOARD_RESIDUE_SCHEMA = "springboard-residue-check.v2.json"


def _springboard_residue_check(shell: ShellFn) -> dict[str, Any]:
    """安装前残留检查的接缝；隔离测试由 ``tests/conftest.py`` 替换为通过的桩。"""
    from autophone.device.springboard_residue import inspect_springboard_residue

    return inspect_springboard_residue(shell)


def _default_shell(sock_path: str) -> ShellFn:
    from autophone.device.shell import ShellError, run_shell

    def run(cmd: str) -> tuple[int, str]:
        try:
            r = run_shell(cmd, timeout_ms=120_000, sock_path=sock_path)
        except ShellError as exc:
            raise InstallError(f"guest shell failed: {exc}") from exc
        return r.code, (r.stdout + r.stderr)

    return run


def verify_registered(bundle_id: str, shell: ShellFn) -> bool:
    """True iff LaunchServices reports `bundle_id` as installed.

    `uicache -i <id>` prints an info block headed by "Bundle Identifier: <id>"
    for a registered app, or "<id> is an invalid bundle id" otherwise — and
    exits 0 either way, so we match on the output, not the code."""
    _code, out = shell(f"uicache -i {bundle_id}")
    return f"Bundle Identifier: {bundle_id}" in out


def _verify_upstream_registered(bundle_id: str, sock_path: str) -> bool:
    from autophone.device.uds import ProtocolError, request

    try:
        result, _ = request(
            {"t": "app_list", "filter": "user", "screen": False},
            sock_path=sock_path, protocol="upstream_v1", parse_image=False,
        )
    except (OSError, ProtocolError) as exc:
        raise InstallError(f"upstream app registration readback failed: {exc}") from exc
    if result.get("ok") is not True:
        raise InstallError(f"upstream app registration readback failed: {result.get('error')}")
    rows = result["apps"]
    return sum(
        row.get("bundle_id", row.get("bundleIdentifier")) == bundle_id
        and isinstance(row.get("path"), str)
        and row["path"].startswith("/")
        for row in rows
    ) == 1


def vphone_native_install(
    guest_ipa_path: str,
    *,
    registration: str = "User",
    sock_path: str | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Install an IPA using vphoned's built-in installer (via UDS).

    This bypasses TrollStore and uses vphone-cli's native installation pipeline:
    extraction → ldid signing → container creation → LaunchServices registration.

    Args:
        guest_ipa_path: Path to IPA on guest filesystem
        registration: "User" or "System" (default: "User")
        sock_path: Explicit vphoned UDS target; defaults to the canonical current target
        timeout_s: Installation timeout (default: 300s for large IPAs)

    Returns:
        Response dict with keys: ok, bundle_id (if successful), msg/detail (if failed)

    Raises:
        InstallError: If installation fails or vphoned returns an error
    """
    from autophone.device.uds import ipa_install, ProtocolError

    logger.info(f"Starting vphone native install: {guest_ipa_path} (registration={registration})")
    selected_sock = sock_path or resolve_install_target_identity()["socket"]
    try:
        resp, sample = ipa_install(
            guest_ipa_path,
            registration=registration,
            sock_path=selected_sock,
            timeout=timeout_s,
        )
    except ProtocolError as exc:
        logger.error(f"vphone native install protocol error: {exc}")
        raise InstallError(f"UDS protocol error during ipa_install: {exc}") from exc
    except Exception as exc:
        logger.error(f"vphone native install failed: {exc}", exc_info=True)
        raise InstallError(f"vphone native install failed: {exc}") from exc

    logger.info(f"vphone native install response: ok={resp.get('ok')}, rt={sample.rt_ms:.0f}ms")

    if not resp.get("ok"):
        msg = resp.get("msg") or resp.get("detail") or resp.get("error") or "unknown error"
        logger.error(f"vphone native install returned ok=false: {msg}")
        raise InstallError(f"vphone native install failed: {msg}")

    # Newer vphoned builds return bundle_id as a field.  The vm build in
    # use on 2026-08-13 returns the same identity only in its canonical success
    # message, so accept that exact grammar and keep all other prose fail-closed.
    bundle_id = resp.get("bundle_id")
    if not str(bundle_id or "").strip():
        matched = _NATIVE_INSTALL_SUCCESS.fullmatch(str(resp.get("msg") or ""))
        if matched is not None:
            bundle_id = matched.group("bundle_id")
    logger.info(f"vphone native install succeeded: bundle_id={bundle_id}")

    return {
        "ok": True,
        "bundle_id": bundle_id,
        "response": resp,
        "rt_ms": sample.rt_ms,
    }


@_locked_package_mutation
@_locked_install_target
def install_package(
    app: str,
    slot: str | None = None,
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
    transfer: TransferFn | None = None,
    shell: ShellFn | None = None,
    installer: InstallerFn | None = None,
    verify: bool = True,
    verify_fn: VerifyFn | None = None,
    build_selector: "BuildSelector | None" = None,
    incoming_dir: str = GUEST_INCOMING,
    helper: str = TROLLSTORE_HELPER,
    keep_remote: bool = False,
    timing: IterationTimingRecorder | None = None,
) -> dict[str, Any]:
    """Upload + install the resolved Build into the VM, then write its receipt.

    Fail-closed: the Installed Build Receipt is written only after (a) the
    native installer reports the expected bundle id AND (b) verify=True
    confirms that bundle id is registered with LaunchServices. A 0 exit alone
    is not evidence the app installed or is launchable, so we re-check
    registration first. Pass verify=False only when the bundle id is unknown or
    the check is genuinely unwanted.

    ``slot`` alone resolves that Version Slot's Candidate; pass
    ``build_selector`` (or use ``install_build``) to pin an exact Build.
    """
    logger.info(f"Starting install: app={app}, slot={slot}, verify={verify}")
    registry = load_registry(app, root=root, code_root=code_root)
    selector = build_selector if build_selector is not None else (
        BuildSelector(slot=str(slot)) if slot is not None else None
    )
    build_ref = _resolve_ref(app, registry, selector, root=root)
    build = registry.build(build_ref.build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_ref.build_id!r}")
    entry = PackageEntry.from_build(build, build_ref.short_version)
    from autophone.device.target import resolve_protocol

    install_target = resolve_install_target_identity()
    socket = install_target["socket"]
    protocol = resolve_protocol(socket)
    shell = shell or _default_shell(socket)
    if transfer is None:
        from autophone.device.uds import ProtocolError

        if protocol == "upstream_v1":
            def transfer(local: Path, remote: str) -> None:
                try:
                    upstream_transfer(local, remote, sock_path=socket, timeout=600.0)
                except (DeviceProtocolError, ProtocolError) as exc:
                    raise TransferError(str(exc)) from exc
        else:
            transfer = scp_to_guest

    # BL-232 阶段 1（I1）：安装前只读检查 SpringBoard 是否带 Frida agent 残留映射。
    # 检出残留或检查本身失败都拒绝安装（裁定甲，fail-closed），发生在任何设备写入与
    # 回执失效之前。控制面 provisioning、交付 deploy（install_in_assignment）与单 VM
    # 兼容路径的包安装都经过这里。
    springboard_residue_check = _springboard_residue_check(shell)
    validate_or_raise(SPRINGBOARD_RESIDUE_SCHEMA, springboard_residue_check)
    if springboard_residue_check["verdict"] != "clean":
        raise SpringBoardResidueRefused(springboard_residue_check)

    # Any install attempt may replace or partially mutate the app currently on
    # the device.  The previous success receipt is no longer admissible until
    # this attempt completes its own registration check.
    install_receipt_path(app, root=root).unlink(missing_ok=True)
    install_fingerprint = install_target_fingerprint(install_target)
    target_receipt_path(app, install_fingerprint, root=root).unlink(missing_ok=True)

    installer = installer or (
        lambda remote_path: vphone_native_install(
            remote_path, registration="User", sock_path=socket, timeout_s=300.0,
        ))
    remote = (
        f"{incoming_dir.rstrip('/')}/"
        f"{app}-{entry.slot}-{uuid.uuid4().hex}.ipa"
    )
    quoted_incoming = shlex.quote(incoming_dir)
    quoted_remote = shlex.quote(remote)

    # guest incoming dir must exist and be writable by the scp user.
    logger.info(f"Preparing guest incoming directory: {incoming_dir}")
    code, out = shell(f"mkdir -p {quoted_incoming} && chmod 777 {quoted_incoming}")
    if code != 0:
        logger.error(f"Failed to prepare incoming dir: code={code}, output={out.strip()[:200]!r}")
        raise InstallError(f"could not prepare {incoming_dir}: {out.strip()[:200]!r}")

    logger.info(
        f"Transferring verified snapshot of {entry.file} → {remote} "
        f"(manifest_size={entry.size} bytes)"
    )
    try:
        entry = _transfer_verified_build(
            app,
            build,
            remote,
            transfer,
            root=root,
            code_root=code_root,
            timing=timing,
        )
        logger.info("Installing via vphone native installer: %s", remote)
        with measure_stage(timing, STAGE_IPA_INSTALL):
            install_result = installer(remote)
            if not isinstance(install_result, Mapping):
                raise InstallError("native installer returned a non-object result")
            installed_bundle_id = install_result.get("bundle_id")
            expected_bundle_id = entry.bundle_id or _profile_bundle_id(
                app, root=root, code_root=code_root,
            )
            if not str(installed_bundle_id or "").strip() and not (
                verify and expected_bundle_id
            ):
                raise InstallError(
                    "native installer response omitted bundle id and no "
                    "LaunchServices verification fallback is available"
                )
            if (
                str(installed_bundle_id or "").strip()
                and expected_bundle_id
                and installed_bundle_id != expected_bundle_id
            ):
                raise InstallError(
                    f"native installer installed bundle {installed_bundle_id!r}, "
                    f"expected {expected_bundle_id!r} for {app}/{entry.slot}"
                )
            logger.info(
                "vphone native install succeeded: bundle_id=%s, rt=%.0fms",
                installed_bundle_id or "unspecified",
                install_result.get("rt_ms", 0),
            )
    finally:
        if not keep_remote:
            try:
                code, _ = shell(f"rm -f {quoted_remote}")
                if code != 0:
                    logger.warning("Could not remove incoming IPA: exit=%s", code)
            except (InstallError, DeviceProtocolError) as exc:
                logger.warning("Could not remove incoming IPA: %s", exc)

    verified = False
    if verify:
        bundle_id = entry.bundle_id or _profile_bundle_id(
            app, root=root, code_root=code_root,
        )
        if not bundle_id:
            raise InstallError(
                f"cannot verify install of slot {entry.slot}: bundle id unknown "
                f"(no IPA metadata, no app profile). Pass verify=False to skip.")
        logger.info(f"Verifying registration: bundle_id={bundle_id}")
        with measure_stage(timing, STAGE_UICACHE):
            if verify_fn is not None:
                registered = verify_fn(bundle_id, shell)
            elif protocol == "upstream_v1":
                registered = _verify_upstream_registered(bundle_id, socket)
            else:
                registered = verify_registered(bundle_id, shell)
            if not registered:
                logger.error(f"Verification failed: {bundle_id!r} not registered with LaunchServices")
                raise InstallError(
                    f"slot {entry.slot} installed but {bundle_id!r} is not registered "
                    "with LaunchServices; active slot left unchanged.")
        logger.info(f"Verification passed: {bundle_id} is registered")
        if not str(installed_bundle_id or "").strip():
            installed_bundle_id = bundle_id
        verified = True

    _write_target_receipt(
        app, build_ref, verified=verified, installer="vphone_native",
        target=install_target, target_fingerprint=install_fingerprint, root=root,
    )
    if registry.source_schema_version == "v1":
        _write_install_receipt(
            app, entry, verified=verified, installer="vphone_native",
            target_identity=install_target, root=root,
        )
    logger.info(f"Install completed successfully: app={app}, slot={entry.slot}, verified={verified}")
    # 这就是 Device Control 返回的 Install Receipt（目标架构 §5.4）：可供兼容、审计与诊断，
    # 权威安装事实由 Provisioning / PackageInstallApplication 回读后另行发布为 Observed State。
    result = {
        "app": app,
        "slot": entry.slot,
        "label": entry.label,
        "bundle_id": entry.bundle_id,
        "version": entry.version,
        "build_id": entry.build_id,
        "build_ref": build_ref.to_dict(),
        "verified": verified,
        "installed_bundle_id": installed_bundle_id,
        "installer": "vphone_native",
        "installed_at": now_iso(),
        "target_fingerprint": install_fingerprint,
        "installed_build": entry.to_dict(),
        "springboard_residue_check": springboard_residue_check,
    }
    if timing is not None and not timing.has_active_stages:
        result["timing"] = timing.to_dict()
    return result


def resolve_build_ref(
    app: str, *, slot: str | None = None, build_id: str | None = None,
    root: Path = ROOT, code_root: Path | None = None,
) -> RegistryBuildRef:
    """Resolve one exact Build for callers that address by slot and/or build id.

    Kept as a thin named entry point so orchestration layers do not have to
    construct a ``BuildSelector`` themselves.
    """
    selector = BuildSelector(slot=slot, build_id=build_id) if (slot or build_id) else None
    return resolve_build(app, selector, root=root, code_root=code_root)


def install_build_ref(
    build_ref: RegistryBuildRef, *, verify: bool = True,
    root: Path = ROOT, code_root: Path | None = None, **kwargs: Any,
) -> dict[str, Any]:
    """Install an already-resolved ref (alias of ``install_build``)."""
    return install_build(
        build_ref, verify=verify, root=root, code_root=code_root, **kwargs,
    )


def install_build(
    build_ref: RegistryBuildRef,
    *,
    verify: bool = True,
    root: Path = ROOT,
    code_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install an already-resolved Build. The Module Interface entry point.

    Passing the ref rather than a selector means the Build that was reviewed is
    the Build that gets installed, even if the Candidate moves in between.
    """
    return install_package(
        build_ref.app,
        build_selector=BuildSelector(
            slot=build_ref.slot, build_id=build_ref.build_id,
        ),
        verify=verify,
        root=root,
        code_root=code_root,
        **kwargs,
    )


def set_default_slot(
    app: str, slot: str, *, root: Path = ROOT,
    code_root: Path | None = None,
) -> None:
    if manifest_schema_version(app, root=root) == "v1":
        raise _v1_write_refused(app, "changing the default package slot")
    get_package(app, slot, root=root, code_root=code_root)
    manifest = load_manifest(app, root=root, code_root=code_root)
    manifest["default_slot"] = slot
    save_manifest(app, manifest, root=root)


# --------------------------------------------------------------------------- #
# remove
# --------------------------------------------------------------------------- #

@_locked_package_mutation
def remove_package(
    app: str, slot: str, *, root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    """Drop a slot's Candidate Build from the store (host-side only; does not
    uninstall from the VM). Compatibility wrapper over ``remove_build``."""
    entry = get_package(app, slot, root=root, code_root=code_root)
    return remove_build(
        app, str(entry.build_id), policy="force", root=root, code_root=code_root,
    )


def build_retention_holds(
    app: str, build_id: str, *, root: Path = ROOT, code_root: Path | None = None,
) -> list[str]:
    """Reasons this Build's bytes must stay available (§12.4).

    Returned as codes rather than a bool so a refusal can say which hold
    applies and a caller can decide whether an explicit override is warranted.
    """
    registry = load_registry(app, root=root, code_root=code_root)
    build = registry.build(build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_id!r}")
    holds: list[str] = []
    if installed_build_id(app, root=root) == build_id:
        holds.append("installed_build")
    slot = registry.slot(build.slot)
    if slot is not None and slot.candidate_build_id == build_id:
        if len(registry.builds_for_slot(build.slot)) > 1:
            holds.append("candidate_build")
    if _accepted_requirement_refs(app, root=root) & {build.sha256}:
        holds.append("accepted_requirement")
    if build.retention_class in ("accepted", "baseline"):
        holds.append(f"retention_class:{build.retention_class}")
    from autophone.packages.updates import base_retention_holds

    holds.extend(base_retention_holds(app, build_id, root=root))
    return holds


@_locked_package_mutation
def promote_candidate(
    app: str,
    build_id: str,
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> dict[str, Any]:
    """Make an existing Build the explicit Candidate of its Version Slot."""
    registry = load_registry(app, root=root, code_root=code_root)
    if registry.source_schema_version != "v2":
        raise PackageError(f"app {app!r} must be migrated to manifest v2 first")
    build = registry.build(build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_id!r}")
    if build.artifact_availability != "present":
        raise PackageError(f"cannot promote build {build_id}: artifact was pruned")
    verify_stored_build(app, build_id, root=root, code_root=code_root)
    updated = registry.with_candidate(build_id)
    save_registry(app, updated, root=root)
    return {"slot": build.slot, "candidate_build_id": build_id}


@_locked_package_mutation
def prune_build_artifact(
    app: str,
    build_id: str,
    *,
    policy: str = "guarded",
    root: Path = ROOT,
    code_root: Path | None = None,
) -> dict[str, Any]:
    """Remove retained bytes while preserving the immutable Build Record."""
    if policy not in ("guarded", "force"):
        raise PackageError(f"unknown prune policy {policy!r}")
    registry = load_registry(app, root=root, code_root=code_root)
    if registry.source_schema_version != "v2":
        raise PackageError(f"app {app!r} must be migrated to manifest v2 first")
    build = registry.build(build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_id!r}")
    if build.artifact_availability == "pruned":
        return {"build_id": build_id, "artifact_availability": "pruned", "duplicate": True}
    from autophone.packages.updates import base_retention_holds

    update_holds = base_retention_holds(app, build_id, root=root)
    if update_holds:
        raise PackageError("retained Update base cannot be removed or pruned: " + ", ".join(update_holds))
    if policy == "guarded":
        holds = build_retention_holds(app, build_id, root=root, code_root=code_root)
        if holds:
            raise PackageError(
                f"refusing to prune build {build_id}: retention holds " + ", ".join(holds)
            )
    stored = _stored_build_path(app, build, root=root, code_root=code_root)
    updated = registry.with_artifact(build_id, availability="pruned")
    save_registry(app, updated, root=root)
    shared = any(
        item.build_id != build_id
        and item.artifact_availability == "present"
        and item.artifact_store_id == build.artifact_store_id
        and item.file == build.file
        for item in updated.builds
    )
    if not shared and stored.is_file():
        stored.unlink()
    return {"build_id": build_id, "artifact_availability": "pruned", "duplicate": False}


@_locked_package_mutation
def relocate_build_artifact(
    app: str,
    build_id: str,
    target_store_id: str,
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> dict[str, Any]:
    """Copy a verified artifact to another store and atomically switch its locator.

    The source copy is retained.  Deleting it is a separate prune operation so
    migration and retention decisions remain independently reviewable.
    """
    registry = load_registry(app, root=root, code_root=code_root)
    if registry.source_schema_version != "v2":
        raise PackageError(f"app {app!r} must be migrated to manifest v2 first")
    build = registry.build(build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_id!r}")
    if build.artifact_availability != "present":
        raise PackageError(f"cannot relocate build {build_id}: artifact was pruned")
    source = _stored_build_path(app, build, root=root, code_root=code_root)
    _verify_stored_build_file(
        app, build, source, backfill_release_metadata=True,
        root=root, code_root=code_root,
    )
    target_store = resolve_artifact_store(
        app, target_store_id, state_root=root, code_root=code_root,
    )
    target_key = (
        f"{BLOBS_DIRNAME}/{build.sha256}.ipa"
        if target_store.store_id == "central"
        else f"{BLOBS_DIRNAME}/sha256/{build.sha256}.ipa"
    )
    target = target_store.path(target_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        if _sha256(temporary) != build.sha256 or temporary.stat().st_size != build.size:
            temporary.unlink(missing_ok=True)
            raise PackageError(f"relocated artifact verification failed for build {build_id}")
        temporary.replace(target)
    elif _sha256(target) != build.sha256 or target.stat().st_size != build.size:
        raise PackageError(f"target artifact conflicts with build {build_id}: {target}")
    updated = registry.with_artifact(
        build_id, store_id=target_store.store_id, file=target_key,
    )
    save_registry(app, updated, root=root)
    return {
        "build_id": build_id,
        "store_id": target_store.store_id,
        "file": target_key,
        "sha256": build.sha256,
    }


def _accepted_requirement_refs(app: str, *, root: Path = ROOT) -> set[str]:
    """Artifact digests referenced by accepted requirements."""
    referenced: set[str] = set()
    requirements_root = Path(root) / "requirements"
    if not requirements_root.is_dir():
        return referenced
    for path in sorted(requirements_root.glob("*/requirement.json")):
        document = read_json(path)
        if not isinstance(document, Mapping) or document.get("state") != "accepted":
            continue
        for iteration in sorted((path.parent / "iterations").glob("*/iteration.json")):
            record = read_json(iteration)
            if not isinstance(record, Mapping):
                continue
            build = (record.get("identity") or {}).get("build") or {}
            if str(build.get("app") or "") == app and build.get("sha256"):
                referenced.add(str(build["sha256"]))
    return referenced


@_locked_package_mutation
def remove_build(
    app: str,
    build_id: str,
    *,
    policy: str = "guarded",
    root: Path = ROOT,
    code_root: Path | None = None,
) -> PackageEntry:
    """Remove one Build Record; delete its Blob only if nothing else uses it.

    ``policy="guarded"`` (default) refuses while a retention hold applies;
    ``policy="force"`` is the explicit human override.
    """
    if policy not in ("guarded", "force"):
        raise PackageError(f"unknown removal policy {policy!r}")
    registry = load_registry(app, root=root, code_root=code_root)
    build = registry.build(build_id)
    if build is None:
        raise PackageError(f"app {app!r} has no build {build_id!r}")
    if registry.source_schema_version == "v1":
        raise _v1_write_refused(app, "removing a package build")
    from autophone.packages.updates import base_retention_holds

    update_holds = base_retention_holds(app, build_id, root=root)
    if update_holds:
        raise PackageError("retained Update base cannot be removed or pruned: " + ", ".join(update_holds))
    if policy == "guarded":
        holds = build_retention_holds(app, build_id, root=root, code_root=code_root)
        if holds:
            raise PackageError(
                f"refusing to remove build {build_id}: retention holds "
                + ", ".join(holds)
            )
    entry = PackageEntry.from_build(build, registry.short_version_of(build))
    # Resolve the stored path *before* touching the manifest: a file reference
    # that escapes the store must fail with the registry still intact.
    stored_blob = _stored_build_path(app, build, root=root, code_root=code_root)

    updated = registry.without_build(build_id)
    save_registry(app, updated, root=root)
    remaining = [
        b for b in updated.builds
        if b.artifact_availability == "present"
        and b.artifact_store_id == build.artifact_store_id
        and b.file == build.file
    ]

    receipt = read_json(install_receipt_path(app, root=root))
    if receipt and str(receipt.get("sha256")) == build.sha256 and str(receipt.get("slot")) == (build.legacy_slot or build.slot):
        install_receipt_path(app, root=root).unlink(missing_ok=True)
    for path in sorted(installations_dir(app, root=root).glob("*.json")):
        target_receipt = read_json(path)
        if target_receipt and str((target_receipt.get("build_ref") or {}).get("build_id")) == build_id:
            path.unlink(missing_ok=True)

    if not remaining and stored_blob.is_file():
        stored_blob.unlink()
    return entry


# --------------------------------------------------------------------------- #
# catalog (read model for CLI / HTTP / page)
# --------------------------------------------------------------------------- #

def catalog(
    app: str,
    *,
    target_fingerprint: str | None = None,
    root: Path = ROOT,
    code_root: Path | None = None,
) -> dict[str, Any]:
    """Version-Slot-grouped read model. Callers never parse the manifest.

    ``installed`` is what the platform last *verifiably installed* on the
    Target — not a claim about what the device is running now.  Confirming the
    device needs a Device Build Observation, which lives with the run.
    """
    registry = load_registry(app, root=root, code_root=code_root)
    receipt = load_target_receipt(app, target_fingerprint=target_fingerprint, root=root)
    installed: dict[str, Any] | None = None
    if receipt:
        installed = {
            "build_ref": receipt.get("build_ref"),
            "verified": bool(receipt.get("verified")),
            "installed_at": receipt.get("installed_at"),
            "target_fingerprint": receipt.get("target_fingerprint"),
            "device_confirmed": False,
            "source": "install_receipt",
        }
    else:
        # A v1 store records "what is installed" as `active_slot`.  Reported as
        # such — with no receipt, and therefore no verification evidence.
        legacy_id = _active_slot_build_id(app, registry, root=root)
        legacy_build = registry.build(legacy_id or "")
        if legacy_build is not None:
            installed = {
                "build_ref": RegistryBuildRef.from_build(
                    app, legacy_build, registry.short_version_of(legacy_build),
                    resolution_source="legacy_alias",
                ).to_dict(),
                "verified": None,
                "installed_at": None,
                "target_fingerprint": None,
                "device_confirmed": False,
                "source": "legacy_default_slot",
            }
    installed_id = (
        str((installed.get("build_ref") or {}).get("build_id") or "") if installed else None
    )
    entries = {e.build_id: e for e in _entries(registry)}

    def build_view(build: BuildRecord) -> dict[str, Any]:
        """Project one Build with this host's byte-availability observation.

        ``artifact_availability`` remains the persisted registry fact.  The
        host observation is deliberately existence-only: integrity checks stay
        on install/promote/verify paths and the catalog does not rewrite state.
        """

        try:
            present = _stored_build_path(
                app, build, root=root, code_root=code_root,
            ).is_file()
        except PackageError:
            present = False
        return {
            **entries[build.build_id].to_view(),
            "artifact_present_on_host": present,
        }

    slots = []
    for slot in registry.version_slots:
        # Newest first for display; registration order, not `added_at`.
        builds = list(reversed(registry.builds_for_slot(slot.slot)))
        candidate = registry.build(slot.candidate_build_id or "")
        slots.append({
            "slot": slot.slot,
            "short_version": slot.short_version,
            "archived": slot.archived,
            "created_at": slot.created_at,
            "build_count": len(builds),
            "candidate_build_id": slot.candidate_build_id,
            "candidate": build_view(candidate) if candidate else None,
            "installed_build_id": (
                installed_id if installed_id in {b.build_id for b in builds} else None
            ),
            "builds": [build_view(b) for b in builds],
        })
    return {
        "app": app,
        "bundle_id": registry.bundle_id,
        "schema_version": registry.source_schema_version,
        "version_slots": slots,
        # Default selection is an Asset-level choice; it never claims what any
        # Rig has installed.  Kept next to ``installed`` so callers can show the
        # two facts side by side instead of conflating them.
        "default_slot": (read_json(manifest_path(app, root=root)) or {}).get(
            "default_slot"
        ),
        "stats": {
            "version_count": len(registry.version_slots),
            "build_count": len(registry.builds),
            "blob_bytes": registry.blob_bytes(),
        },
        "installed": installed,
    }
