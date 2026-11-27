"""Automated layering guard enforcing the ``autophone-kernel`` import boundary.

Ensures every module owned by ``autophone-kernel`` (per ``pkgs/ownership.py``)
has ZERO top-level, function-local, or dynamic-literal imports targeting
``autophone-rig``, ``autophone-domain``, ``autophone-console``, or
``autophone-delivery``, and verifies that kernel asset modules import and run
core validation cleanly when upper layers are absent.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from autophone.assets import _package_manifest
from autophone.packages.model import BuildSelector
from autophone.packages.store import resolve_build
from pkgs.ownership import owner, source_files
from scripts import verify_core_archives

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_LAYERS = frozenset({"rig", "domain", "console", "delivery"})


def _build_module_owner_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in source_files(ROOT):
        rel = path.relative_to(ROOT).as_posix()
        if not rel.endswith(".py"):
            continue
        comp = owner(rel)
        if comp is None:
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            mapping[".".join(parts)] = comp
    return mapping


def _resolve_owner(module_name: str, mod_owner: dict[str, str]) -> tuple[str | None, str | None]:
    cur = module_name
    while cur:
        if cur in mod_owner:
            return mod_owner[cur], cur
        if "." not in cur:
            break
        cur = cur.rsplit(".", 1)[0]
    return None, None


def _scan_kernel_imports() -> tuple[list[str], list[str]]:
    mod_owner = _build_module_owner_map()
    kernel_files: list[str] = []
    violations: list[str] = []

    for path in source_files(ROOT):
        rel = path.relative_to(ROOT).as_posix()
        if not rel.endswith(".py") or owner(rel) != "kernel":
            continue
        kernel_files.append(rel)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        pkg_parts = rel[:-3].split("/")[:-1]

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    comp, matched = _resolve_owner(alias.name, mod_owner)
                    if comp in FORBIDDEN_LAYERS:
                        violations.append(
                            f"{rel}:{node.lineno}: import {alias.name} -> {comp} ({matched})"
                        )
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    prefix_parts = pkg_parts[: len(pkg_parts) - node.level + 1]
                    if base:
                        prefix_parts.append(base)
                    full_base = ".".join(prefix_parts)
                else:
                    full_base = base
                comp, matched = _resolve_owner(full_base, mod_owner)
                if comp in FORBIDDEN_LAYERS:
                    violations.append(
                        f"{rel}:{node.lineno}: from {full_base} import ... -> {comp} ({matched})"
                    )
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sub = f"{full_base}.{alias.name}" if full_base else alias.name
                    sub_comp, sub_matched = _resolve_owner(sub, mod_owner)
                    if sub_comp in FORBIDDEN_LAYERS:
                        violations.append(
                            f"{rel}:{node.lineno}: from {full_base} import {alias.name} "
                            f"-> {sub_comp} ({sub_matched})"
                        )
            elif isinstance(node, ast.Call):
                func = node.func
                is_dynamic = (
                    (isinstance(func, ast.Name) and func.id == "__import__")
                    or (isinstance(func, ast.Attribute) and func.attr == "import_module")
                )
                if is_dynamic and node.args and isinstance(node.args[0], ast.Constant):
                    target = node.args[0].value
                    if isinstance(target, str):
                        comp, matched = _resolve_owner(target, mod_owner)
                        if comp in FORBIDDEN_LAYERS:
                            violations.append(
                                f"{rel}:{node.lineno}: dynamic import {target!r} -> {comp} ({matched})"
                            )

    return kernel_files, violations


def test_kernel_has_zero_imports_of_upper_layers() -> None:
    kernel_files, violations = _scan_kernel_imports()
    assert len(kernel_files) >= 40, f"unexpectedly small kernel file set: {len(kernel_files)}"
    assert "autophone/assets/app_symbols.py" in kernel_files
    assert "autophone/assets/symbol_tools.py" in kernel_files
    assert "autophone/assets/entity_binding.py" in kernel_files
    assert violations == [], "autophone-kernel layering violations found:\n" + "\n".join(violations)


def test_kernel_asset_modules_run_without_domain_rig_or_console(tmp_path: Path) -> None:
    """Block all non-kernel autophone.* modules in a fresh Python subprocess and run kernel validation."""
    mod_owner = _build_module_owner_map()
    blocked_modules = sorted(
        mod for mod, comp in mod_owner.items() if comp in FORBIDDEN_LAYERS
    )

    # Create a minimal app profile + v2 package manifest + symbol binding + operations.yaml
    app_dir = tmp_path / "apps" / "demo_kernel"
    (app_dir / "operations").mkdir(parents=True)
    (app_dir / "symbols" / "artifacts").mkdir(parents=True)
    sha = "a" * 64
    build_id = "bld-" + "b" * 24

    (app_dir / "profile.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "v1",
            "app": "demo_kernel",
            "display_name": "Demo Kernel",
            "bundle_id": "com.example.demokernel",
        }),
        encoding="utf-8",
    )
    (app_dir / "operations" / "operations.yaml").write_text(
        yaml.safe_dump({
            "meta": {"schema_version": 1, "app": "demo_kernel", "next_seq": 3},
            "operations": [
                {
                    "id": "OP1",
                    "status": "active",
                    "revision": 1,
                    "name": "read_state",
                    "summary": "Read-only check",
                    "params": [{"name": "item_id", "type": "string", "required": True}],
                    "steps": [{"action": "assert_present", "queries": ["状态"]}],
                    "navigation": {"safety": "read_only"},
                    "provenance": {"source": "observed"},
                },
                {
                    "id": "OP2",
                    "status": "active",
                    "revision": 1,
                    "name": "pay_now",
                    "summary": "Irreversible step",
                    "params": [{"name": "item_id", "type": "string", "required": True}],
                    "steps": [{"action": "tap_text", "query": "确认支付"}],
                    "provenance": {"source": "observed"},
                },
            ],
        }),
        encoding="utf-8",
    )
    (app_dir / "symbols" / "artifacts" / f"{sha[:12]}.yaml").write_text(
        yaml.safe_dump({
            "schema": "autophone.app-symbols/1",
            "app": "demo_kernel",
            "artifact_identity": {
                "kind": "ipa_build",
                "bundle_id": "com.example.demokernel",
                "short_version": "1.0.0",
                "build_version": "10",
                "build_id": build_id,
                "artifact_sha256": sha,
            },
            "controls": {
                "ctrl.home.submit": {
                    "kind": "button",
                    "scope": {"screen_id": "home"},
                    "locator": {
                        "kind": "ax_identifier",
                        "identifier": "home.submit",
                        "role": "Button",
                        "enabled_only": True,
                        "require_unique": True,
                    },
                    "effects": {"verbs": [], "completeness": "exhaustive"},
                }
            },
        }),
        encoding="utf-8",
    )
    pkg_dir = tmp_path / "state" / "packages" / "demo_kernel"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "manifest.json").write_text(
        json.dumps({
            "schema_version": "v2",
            "app": "demo_kernel",
            "bundle_id": "com.example.demokernel",
            "default_slot": "001",
            "version_slots": [{
                "slot": "001",
                "short_version": "1.0.0",
                "candidate_build_id": build_id,
                "created_at": "2026-01-01T00:00:00Z",
            }],
            "builds": [{
                "build_id": build_id,
                "slot": "001",
                "label": "1.0.0",
                "bundle_id": "com.example.demokernel",
                "build_version": "10",
                "source_revision": {"kind": "unknown", "state": "unknown"},
                "artifact": {"file": "001.ipa", "sha256": sha, "size": 123},
                "artifact_availability": "present",
                "release_like": False,
                "release_evidence": None,
                "legacy_slot": None,
                "added_at": "2026-01-01T00:00:00Z",
            }],
            "legacy_slot_aliases": {},
        }),
        encoding="utf-8",
    )

    script = f"""
import importlib.abc
import json
from pathlib import Path
import sys

BLOCKED = set({blocked_modules!r})

class _UpperLayerBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in BLOCKED:
            raise ModuleNotFoundError(f"blocked upper-layer module: {{fullname}}", name=fullname)
        return None

sys.meta_path.insert(0, _UpperLayerBlocker())

from autophone.assets.apps import load_app_profile
from autophone.assets import app_symbols, case_lint, entity_binding, symbol_tools

root = Path({str(tmp_path)!r})
state_root = root / "state"
profile = load_app_profile("demo_kernel", root=root)

# 1. app_symbols: resolve default build + explicit build_id + load symbol binding
resolved = app_symbols.resolve_artifact_identity_for_app(
    profile, package_root=state_root, allow_default_build=True,
)
assert resolved.artifact_content_digest == {sha!r}
assert resolved.build_id == {build_id!r}
loaded = app_symbols.load_symbol_binding_for_app(
    profile, build_id={build_id!r}, package_root=state_root,
)
assert loaded.require_control("ctrl.home.submit").locator.identifier == "home.submit"

# 2. symbol_tools: import L2 manifest using Package Store build_id without autophone.packages
res = symbol_tools.import_l2_manifest(
    "demo_kernel",
    {{
        "app": "demo_kernel",
        "controls": {{
            "ctrl.home.submit": {{
                "kind": "button",
                "scope": {{"screen_id": "home"}},
                "locator": {{"kind": "ax_identifier", "identifier": "home.submit"}},
                "effects": {{"verbs": [], "completeness": "exhaustive"}},
            }}
        }},
    }},
    build_id={build_id!r},
    output_root=root / "out",
    package_root=state_root,
    code_root=root,
)
assert res["artifact_content_digest"] == {sha!r}
assert res["control_count"] == 1

# 3. entity_binding + case_lint: static API assertion check + YAML readback safety check
good_case = {{
    "id": "R1",
    "status": "active",
    "automation": "ui_ready",
    "expects": ["ok"],
    "entity_binding": {{
        "entity_type": "item",
        "entity_id": "item-1",
        "api_window": {{
            "expect": [{{
                "method": "GET",
                "path": "/items/${{entity.id}}",
                "assert": {{"status": 200, "resp.data.id": "${{entity.id}}"}},
            }}],
        }},
        "readback": {{
            "op": "read_state",
            "with": {{"item_id": {{"from_entity": "id"}}}},
            "expect_context": {{"item_id": {{"from_entity": "id"}}}},
        }},
    }},
}}
assert entity_binding.validate_case_entity_binding(profile, good_case) == []

bad_irrev = dict(good_case)
bad_irrev["entity_binding"] = {{
    **good_case["entity_binding"],
    "readback": {{
        "op": "pay_now",
        "with": {{"item_id": {{"from_entity": "id"}}}},
        "expect_context": {{"item_id": {{"from_entity": "id"}}}},
    }},
}}
issues = entity_binding.validate_case_entity_binding(profile, bad_irrev)
assert any(i.code == "entity_binding_readback_op_irreversible" for i in issues), issues

bad_key_doc = {{
    "meta": {{"schema_version": 2, "total": 1, "next_seq": 2}},
    "cases": [{{
        **good_case,
        "entity_binding": {{
            "entity_type": "item",
            "entity_id": "item-1",
            "api_window": {{
                "expect": [{{
                    "method": "GET",
                    "path": "/x",
                    "assert": {{"invalid.prefix": "1"}},
                }}],
            }},
        }},
    }}],
}}
report = case_lint.lint_case_registry_document(bad_key_doc)
assert any(e["code"] == "entity_binding_invalid_assert_key" for e in report["errors"])
print("KERNEL_STANDALONE_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "KERNEL_STANDALONE_OK" in proc.stdout


def test_kernel_package_manifest_reader_matches_domain_store_v1_and_v2(tmp_path: Path) -> None:
    """Verify kernel manifest reader agrees with ``autophone.packages.store.resolve_build``."""
    sha1 = hashlib.sha256(b"ipa-v1").hexdigest()
    sha2 = hashlib.sha256(b"ipa-v2").hexdigest()

    # v1 manifest with legacy active_slot
    v1_dir = tmp_path / "packages" / "demo_v1"
    v1_dir.mkdir(parents=True)
    (v1_dir / "manifest.json").write_text(
        json.dumps({
            "schema_version": "v1",
            "app": "demo_v1",
            "bundle_id": "com.example.v1",
            "active_slot": "001",
            "packages": [{
                "slot": "001",
                "label": "1.0.0",
                "file": "001.ipa",
                "bundle_id": "com.example.v1",
                "version": "1.0.0",
                "release_like": False,
                "release_evidence": None,
                "sha256": sha1,
                "size": 100,
                "added_at": "2026-01-01T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )

    domain_v1 = resolve_build("demo_v1", None, root=tmp_path)
    kernel_v1 = _package_manifest.resolve_package_manifest_build("demo_v1", root=tmp_path)
    assert kernel_v1.build_id == domain_v1.build_id
    assert kernel_v1.artifact_sha256 == domain_v1.artifact_sha256 == sha1
    assert kernel_v1.short_version == domain_v1.short_version == "1.0.0"
    assert kernel_v1.bundle_id == "com.example.v1"

    domain_v1_exact = resolve_build("demo_v1", BuildSelector(build_id=domain_v1.build_id), root=tmp_path)
    kernel_v1_exact = _package_manifest.resolve_package_manifest_build(
        "demo_v1", build_id=domain_v1.build_id, root=tmp_path,
    )
    assert kernel_v1_exact.build_id == domain_v1_exact.build_id
    assert kernel_v1_exact.artifact_sha256 == domain_v1_exact.artifact_sha256

    # v2 manifest with default_slot + multiple builds
    v2_dir = tmp_path / "packages" / "demo_v2"
    v2_dir.mkdir(parents=True)
    bld1 = "bld-" + "1" * 24
    bld2 = "bld-" + "2" * 24
    (v2_dir / "manifest.json").write_text(
        json.dumps({
            "schema_version": "v2",
            "app": "demo_v2",
            "bundle_id": "com.example.v2",
            "default_slot": "001",
            "version_slots": [
                {
                    "slot": "001",
                    "short_version": "1.0.0",
                    "candidate_build_id": bld1,
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "slot": "002",
                    "short_version": "2.0.0",
                    "candidate_build_id": bld2,
                    "created_at": "2026-01-02T00:00:00Z",
                },
            ],
            "builds": [
                {
                    "build_id": bld1,
                    "slot": "001",
                    "label": "1.0.0",
                    "bundle_id": "com.example.v2",
                    "build_version": "10",
                    "source_revision": {"kind": "unknown", "state": "unknown"},
                    "artifact": {"file": "001.ipa", "sha256": sha1, "size": 100},
                    "artifact_availability": "present",
                    "release_like": False,
                    "release_evidence": None,
                    "legacy_slot": None,
                    "added_at": "2026-01-01T00:00:00Z",
                },
                {
                    "build_id": bld2,
                    "slot": "002",
                    "label": "2.0.0",
                    "bundle_id": "com.example.v2.beta",
                    "build_version": "20",
                    "source_revision": {"kind": "unknown", "state": "unknown"},
                    "artifact": {"file": "002.ipa", "sha256": sha2, "size": 200},
                    "artifact_availability": "present",
                    "release_like": False,
                    "release_evidence": None,
                    "legacy_slot": None,
                    "added_at": "2026-01-02T00:00:00Z",
                },
            ],
            "legacy_slot_aliases": {},
        }),
        encoding="utf-8",
    )

    domain_v2_def = resolve_build("demo_v2", None, root=tmp_path)
    kernel_v2_def = _package_manifest.resolve_package_manifest_build("demo_v2", root=tmp_path)
    assert kernel_v2_def.build_id == domain_v2_def.build_id == bld1
    assert kernel_v2_def.artifact_sha256 == domain_v2_def.artifact_sha256 == sha1
    assert kernel_v2_def.build_version == domain_v2_def.build_version == "10"

    domain_v2_b2 = resolve_build("demo_v2", BuildSelector(build_id=bld2), root=tmp_path)
    kernel_v2_b2 = _package_manifest.resolve_package_manifest_build(
        "demo_v2", build_id=bld2, root=tmp_path,
    )
    assert kernel_v2_b2.build_id == domain_v2_b2.build_id == bld2
    assert kernel_v2_b2.artifact_sha256 == domain_v2_b2.artifact_sha256 == sha2
    assert kernel_v2_b2.short_version == domain_v2_b2.short_version == "2.0.0"
    assert kernel_v2_b2.bundle_id == "com.example.v2.beta"
    assert _package_manifest.lookup_package_manifest_bundle_id("demo_v2", bld2, root=tmp_path) == "com.example.v2.beta"

    with pytest.raises(_package_manifest.PackageManifestError):
        _package_manifest.resolve_package_manifest_build("demo_v2", build_id="bld-" + "f" * 24, root=tmp_path)
    with pytest.raises(_package_manifest.PackageManifestError):
        _package_manifest.resolve_package_manifest_build("../escape", root=tmp_path)


def test_verify_core_archives_enforces_kernel_layering_boundary(tmp_path: Path) -> None:
    """Verify build-time archive verification rejects a kernel wheel importing domain."""
    from tests.test_public_packaging_hygiene import _create_synthetic_archive_set

    schema_root = tmp_path / "repo"
    dist_dir = tmp_path / "dist"
    _create_synthetic_archive_set(
        dist_dir,
        schema_root,
        extra_wheel_files={
            "autophone-kernel": {
                "autophone/assets/bad_kernel.py": (
                    b"def f():\n"
                    b"    from autophone.packages.store import resolve_build\n"
                    b"    return resolve_build\n"
                ),
            },
            "autophone-domain": {
                "autophone/packages/store.py": b"def resolve_build(): pass\n",
            },
        },
    )
    with pytest.raises(ValueError, match="kernel layering violation"):
        verify_core_archives.verify(dist_dir, root=schema_root)
