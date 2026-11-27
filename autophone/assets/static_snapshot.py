"""Canonical IPA static snapshot used by the App knowledge differ."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from autophone.contracts import validate_or_raise, write_json
from autophone.assets.plist import bundle_identity, parse_plist

RULES = {
    "version": 1,
    "endpoint_patterns": [r"https?://[^\\s\\\"'<>]+", r"/(?:api|rest|gateway)/[A-Za-z0-9_./?=&%:-]+"],
    "rn_suffixes": [".jsbundle", ".bundle", ".js"],
    "resource_terms": ["urllaunch", "cordova", "react", "rn"],
}
_URL = re.compile(rb"https?://[^\s\x00\"'<>]+")
_API_PATH = re.compile(rb"/(?:api|rest|gateway)/[A-Za-z0-9_./?=&%:-]+")


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _detector(scope: str, status: str, observations: Any, reason: str | None = None) -> dict[str, Any]:
    row = {
        "scope": scope,
        "status": status,
        "observations": observations,
        "digest": _digest(observations),
    }
    if reason:
        row["reason"] = reason
    return row


def _app_root(names: list[str]) -> str:
    matches = sorted(
        name.removesuffix("Info.plist")
        for name in names
        if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)
    )
    if len(matches) != 1:
        raise ValueError(f"expected one app Info.plist, found {len(matches)}")
    return matches[0]


def _mobileprovision_entitlements(raw: bytes) -> dict[str, Any] | None:
    start = raw.find(b"<?xml")
    end = raw.find(b"</plist>", start)
    if start < 0 or end < 0:
        return None
    plist = parse_plist(raw[start:end + len(b"</plist>")])
    entitlements = plist.get("Entitlements")
    return _jsonable(entitlements) if isinstance(entitlements, dict) else None


def snapshot(ipa: Path) -> dict[str, Any]:
    if not ipa.is_file() or not zipfile.is_zipfile(ipa):
        raise ValueError(f"not a readable IPA: {ipa}")
    package_sha = _file_sha(ipa)
    with zipfile.ZipFile(ipa) as archive:
        names = sorted(archive.namelist())
        root = _app_root(names)
        plist = parse_plist(archive.read(f"{root}Info.plist"))
        identity = bundle_identity(plist)
        executable = identity["executable"]
        primary_path = f"{root}{executable}" if executable else ""
        primary = archive.read(primary_path) if primary_path in names else None

        resource_rows = [
            {
                "path": name,
                "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
                "size": archive.getinfo(name).file_size,
            }
            for name in names
            if not name.endswith("/")
            and any(term in name.lower() for term in RULES["resource_terms"])
        ]
        rn_rows = [
            {
                "path": name,
                "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
                "size": archive.getinfo(name).file_size,
            }
            for name in names
            if not name.endswith("/")
            and name.lower().endswith(tuple(RULES["rn_suffixes"]))
        ]

        endpoints: set[str] = set()
        for name in names:
            if name.endswith("/") or archive.getinfo(name).file_size > 24 * 1024 * 1024:
                continue
            raw = archive.read(name)
            for match in (*_URL.findall(raw), *_API_PATH.findall(raw)):
                endpoints.add(match.decode("utf-8", errors="ignore")[:2048])

        provision_path = f"{root}embedded.mobileprovision"
        if provision_path in names:
            provision_raw = archive.read(provision_path)
            entitlements = _mobileprovision_entitlements(provision_raw)
            entitlements_detector = _detector(
                "entitlements",
                "complete" if entitlements is not None else "partial",
                entitlements or {"mobileprovision_sha256": hashlib.sha256(provision_raw).hexdigest()},
                None if entitlements is not None else "mobileprovision_entitlements_unparsed",
            )
        else:
            entitlements_detector = _detector(
                "entitlements", "unavailable", [], "embedded_mobileprovision_missing"
            )

    detectors = [
        _detector("bundle_identity", "complete", identity),
        _detector(
            "primary_binary",
            "complete" if primary is not None else "unavailable",
            ({"path": primary_path, "sha256": hashlib.sha256(primary).hexdigest(), "size": len(primary)} if primary is not None else []),
            None if primary is not None else "primary_executable_missing",
        ),
        _detector("resources", "complete", resource_rows),
        _detector("rn_bundles", "complete", rn_rows),
        _detector("endpoints", "complete", sorted(endpoints)),
        entitlements_detector,
    ]
    canonical = {
        "schema_version": "v1",
        "package_sha256": package_sha,
        "extractor_rules_sha256": _digest(RULES),
        "detectors": detectors,
    }
    payload = {**canonical, "snapshot_digest": _digest(canonical)}
    validate_or_raise("ipa-static-snapshot.v1.json", payload)
    return payload


def write_snapshot(ipa: Path, output: Path) -> Path:
    return write_json(output, snapshot(ipa))
