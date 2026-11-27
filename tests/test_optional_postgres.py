"""Tests for optional PostgreSQL (psycopg) dependencies and fail-closed behavior."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from autophone.adapters.cli import rig
from autophone.adapters.http.app import create_app
from autophone.application.control_plane import (
    ControlPlaneUnavailable,
    configured_control_plane,
    require_control_plane,
    reset_control_plane_cache,
)
from autophone.application.runnability import fetch_postgres_runs
from autophone.control import PostgresControlStore, PostgresDriverUnavailable
from autophone.control._psycopg import (
    POSTGRES_INSTALL_HINT,
    psycopg_error_types,
    psycopg_errors_by_name,
    require_psycopg,
)
from autophone.control.http import configured_app
from scripts import verify_core_archives
from tests.test_public_packaging_hygiene import _create_synthetic_archive_set

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_PACKAGES = frozenset({"psycopg", "psycopg-binary", "psycopg-c", "psycopg-pool"})
PYPROJECT_FILES = (
    ROOT / "pyproject.toml",
    ROOT / "pkgs" / "autophone-kernel" / "pyproject.toml",
    ROOT / "pkgs" / "autophone-rig" / "pyproject.toml",
    ROOT / "pkgs" / "autophone-domain" / "pyproject.toml",
    ROOT / "pkgs" / "autophone-console" / "pyproject.toml",
)


def _req_name(req: str) -> str:
    head = req.split(";", 1)[0].strip()
    raw = re.split(r"[\s\[(<>=!~@]", head, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", raw).lower()


def _block_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all ``psycopg*`` imports inside the current process."""
    for name in list(sys.modules):
        if name == "psycopg" or name.startswith(("psycopg.", "psycopg_binary", "psycopg_c")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    for blocked in (
        "psycopg",
        "psycopg.sql",
        "psycopg.rows",
        "psycopg.errors",
        "psycopg.types",
        "psycopg.types.json",
        "psycopg_binary",
        "psycopg_c",
    ):
        monkeypatch.setitem(sys.modules, blocked, None)


# ── 1. Packaging & metadata contract ─────────────────────────────────────────


def test_core_manifests_exclude_psycopg_from_base_deps_and_expose_postgres_extra() -> None:
    for path in PYPROJECT_FILES:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data["project"]
        pkg_name = project["name"]
        base_deps = project.get("dependencies") or []
        for dep in base_deps:
            assert _req_name(dep) not in POSTGRES_PACKAGES, (
                f"{path.relative_to(ROOT)} must not require {dep!r} in base dependencies"
            )

        optional = project.get("optional-dependencies") or {}
        if pkg_name in {"autophone", "autophone-rig"}:
            assert "postgres" in optional, f"{pkg_name} must define optional extra 'postgres'"
            assert any(_req_name(dep) == "psycopg" for dep in optional["postgres"]), (
                f"{pkg_name}[postgres] must declare psycopg"
            )

    root_data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    root_optional = root_data["project"]["optional-dependencies"]
    assert any("postgres" in item for item in root_optional["all"])
    dep_groups = root_data["dependency-groups"]
    assert any(_req_name(dep) == "psycopg" for dep in dep_groups["postgres"])
    assert any(_req_name(dep) == "psycopg" for dep in dep_groups["platform"])


@pytest.mark.parametrize("archive_kind", ["whl", "sdist"])
def test_verify_core_archives_rejects_base_psycopg_and_missing_postgres_extra(
    tmp_path: Path, archive_kind: str,
) -> None:
    schema_root = tmp_path / "repo"
    bad_base_dir = tmp_path / "bad-base"
    if archive_kind == "whl":
        extra_whl = {
            "autophone-rig": {
                "autophone_rig-0.1.0.dist-info/METADATA": (
                    b"Name: autophone-rig\nVersion: 0.1.0\n"
                    b"Requires-Dist: psycopg[binary]>=3.2\n"
                    b"Provides-Extra: postgres\n"
                    b'Requires-Dist: psycopg[binary]>=3.2; extra == "postgres"\n'
                ),
            },
        }
        _create_synthetic_archive_set(bad_base_dir, schema_root, extra_wheel_files=extra_whl)
    else:
        extra_sdist = {
            "autophone-rig": {
                "pyproject.toml": (
                    b'[project]\nname = "autophone-rig"\nversion = "0.1.0"\n'
                    b'dependencies = ["psycopg[binary]>=3.2"]\n'
                    b'[project.optional-dependencies]\npostgres = ["psycopg[binary]>=3.2"]\n'
                ),
            },
        }
        _create_synthetic_archive_set(bad_base_dir, schema_root, extra_sdist_files=extra_sdist)

    with pytest.raises(ValueError, match="must be optional"):
        verify_core_archives.verify(bad_base_dir, root=schema_root)

    missing_extra_dir = tmp_path / "missing-extra"
    if archive_kind == "whl":
        extra_whl = {
            "autophone": {
                "autophone-0.1.0.dist-info/METADATA": b"Name: autophone\nVersion: 0.1.0\n",
            },
        }
        _create_synthetic_archive_set(missing_extra_dir, schema_root, extra_wheel_files=extra_whl)
    else:
        extra_sdist = {
            "autophone": {
                "pyproject.toml": b'[project]\nname = "autophone"\nversion = "0.1.0"\n',
            },
        }
        _create_synthetic_archive_set(missing_extra_dir, schema_root, extra_sdist_files=extra_sdist)

    with pytest.raises(ValueError, match="missing required 'postgres' optional extra"):
        verify_core_archives.verify(missing_extra_dir, root=schema_root)


def test_build_public_core_copies_complete_food_ordering_example_tree() -> None:
    script_text = (ROOT / "scripts" / "build_public_core.sh").read_text(encoding="utf-8")
    food_root = ROOT / "examples" / "food-ordering" / "apps" / "food_ordering"
    expected_files = sorted(
        path.relative_to(food_root).as_posix()
        for path in food_root.rglob("*")
        if path.is_file()
    )
    assert expected_files
    for rel in expected_files:
        assert rel in script_text, f"scripts/build_public_core.sh omits food-ordering asset {rel!r}"


# ── 2. Clean imports & local single-VM mode without psycopg ──────────────────


def test_core_imports_cli_and_local_dashboard_succeed_without_psycopg(tmp_path: Path) -> None:
    """Boot a fresh interpreter that forbids any ``psycopg`` import and run local workflows."""
    script = """
import importlib.abc
import json
import os
from pathlib import Path
import sys
from fastapi.testclient import TestClient


class BlockPsycopg(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".", 1)[0]
        if top in {"psycopg", "psycopg_binary", "psycopg_c"}:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, BlockPsycopg())

import autophone
import autophone.cli
import autophone.control
import autophone.control.agent_gateway
import autophone.control.http
import autophone.control.interrupted_closure
import autophone.control.postgres
import autophone.control.recovery
import autophone.control.reset
import autophone.adapters.cli.rig
import autophone.adapters.http.app
import autophone.adapters.http.devices
import autophone.application.control_plane
import autophone.application.runnability
import autophone.vm_fatal

assert "psycopg" not in sys.modules
assert autophone.application.control_plane.configured_control_plane() is None

app = autophone.adapters.http.app.create_app()
with TestClient(app) as client:
    assert client.get("/health").json()["ok"] is True
    targets = client.get("/api/platform/targets").json()
    assert targets["mode"] == "local", targets

# Exercise rig init without psycopg.
scratch = Path(os.environ["TEST_SCRATCH"])
bundle = scratch / "VPhone.bundle"
(bundle / "Contents" / "MacOS").mkdir(parents=True)
vm_dir = scratch / "machines" / "rig-a"
vm_dir.mkdir(parents=True)
(vm_dir / "udid-prediction.txt").write_text("UDID: 00008101-000A11112222333A\\n", encoding="utf-8")
target, out_path = autophone.adapters.cli.rig.init_target(
    "rig-a", bundle=bundle, vm_dir=vm_dir, host_id="local-mac",
)
assert out_path.is_file() and target.rig_id == "rig-a"

# Exercise CLI parser and app asset commands without psycopg.
parser = autophone.cli.build_parser(include_extensions=False)
assert "rig" in parser.format_help()
assert autophone.cli.main(["app", "list"]) == 0
assert autophone.cli.main(["app", "show", "food_ordering"]) == 0
assert autophone.cli.main(["app", "case", "lint", "food_ordering"]) == 0

assert "psycopg" not in sys.modules
print(json.dumps({"ok": True}))
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AUTOPHONE_", "VPHONE_"))
    }
    env.update(
        AUTOPHONE_STATE_DIR=str(tmp_path / "state"),
        AUTOPHONE_APP_ASSET_DIR=str(ROOT / "examples" / "food-ordering"),
        AUTOPHONE_SCHEDULER_ENABLED="0",
        AUTOPHONE_FOREGROUND_ENABLED="0",
        AUTOPHONE_DISABLE_EXTENSIONS="1",
        PYTHONPATH=str(ROOT),
        TEST_SCRATCH=str(tmp_path / "scratch"),
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"ok": true' in completed.stdout.splitlines()[-1]


def test_rig_up_single_vm_local_mode_does_not_require_psycopg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_psycopg(monkeypatch)
    monkeypatch.delenv("AUTOPHONE_DATABASE_URL", raising=False)
    target = SimpleNamespace(
        rig_id="rig-a",
        protocol="upstream_v1",
        core_bundle=tmp_path / "VPhone.bundle",
        vm_dir=tmp_path / "machines" / "rig-a",
        socket=tmp_path / "machines" / "rig-a" / "vphone.sock",
    )
    called_local: list[str] = []
    monkeypatch.setattr(rig, "configured_targets", lambda _paths: [target])
    monkeypatch.setattr(
        rig,
        "_run_local_single_stack",
        lambda tgt, port, **_kw: called_local.append(f"{tgt.rig_id}:{port}"),
    )
    rig.run_stack([tmp_path / "rig-a.json"], 8100, 8000)
    assert called_local == ["rig-a:8000"]


# ── 3. Actionable fail-closed errors when PostgreSQL is requested ────────────


def test_postgres_entry_points_fail_closed_with_install_hint_when_psycopg_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_screen_lock_root: Path,
) -> None:
    _block_psycopg(monkeypatch)
    dsn = "postgresql://127.0.0.1:5432/autophone"
    monkeypatch.setenv("AUTOPHONE_DATABASE_URL", dsn)
    reset_control_plane_cache()
    try:
        # 1. Low-level lazy loader.
        with pytest.raises(PostgresDriverUnavailable) as loader_exc:
            require_psycopg()
        assert isinstance(loader_exc.value, ImportError)
        assert "pip install 'autophone[postgres]'" in str(loader_exc.value)
        assert "uv sync --extra postgres" in str(loader_exc.value)

        # 2. Direct PostgresControlStore construction.
        with pytest.raises(PostgresDriverUnavailable) as store_exc:
            PostgresControlStore(dsn)
        assert POSTGRES_INSTALL_HINT in str(store_exc.value)

        # 3. configured_control_plane() and require_control_plane() fail closed (never return None).
        for call in (configured_control_plane, require_control_plane):
            with pytest.raises(ControlPlaneUnavailable) as cp_exc:
                call()
            assert isinstance(cp_exc.value.__cause__, PostgresDriverUnavailable)
            assert "pip install 'autophone[postgres]'" in str(cp_exc.value)
            assert "uv sync --extra postgres" in str(cp_exc.value)

        # 4. Standalone Control Plane HTTP app factory.
        with pytest.raises(PostgresDriverUnavailable) as http_exc:
            configured_app()
        assert "autophone[postgres]" in str(http_exc.value)

        # 5. Read-only PostgreSQL runnability query.
        with pytest.raises(PostgresDriverUnavailable) as runnability_exc:
            fetch_postgres_runs(dsn, "autophone", ["RUN-1"])
        assert "autophone[postgres]" in str(runnability_exc.value)

        # 6. rig up with AUTOPHONE_DATABASE_URL or --ephemeral-db fails closed cleanly.
        monkeypatch.setattr(rig, "configured_targets", lambda _paths: [SimpleNamespace(rig_id="rig-a")])
        with pytest.raises(PostgresDriverUnavailable) as stack_exc:
            rig.run_stack([tmp_path / "rig-a.json"], 8100, 8000)
        assert "autophone[postgres]" in str(stack_exc.value)

        monkeypatch.delenv("AUTOPHONE_DATABASE_URL", raising=False)
        with pytest.raises(PostgresDriverUnavailable):
            rig.run_stack([tmp_path / "rig-a.json"], 8100, 8000, ephemeral_db=True)

        args = SimpleNamespace(
            target=[tmp_path / "rig-a.json"],
            control_port=8100,
            dashboard_port=8000,
            allow_multi_vm=False,
            springboard_region_probe=None,
            ephemeral_db=True,
            decline_usb_trust=False,
            usb_trust_wait=25.0,
        )
        assert rig.cmd_up(args) == 2
        assert "autophone[postgres]" in capsys.readouterr().err

        # 7. Dashboard HTTP API fails closed with 503 (never falls back to local mode).
        monkeypatch.setenv("AUTOPHONE_DATABASE_URL", dsn)
        monkeypatch.setenv("AUTOPHONE_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("AUTOPHONE_SCHEDULER_ENABLED", "0")
        monkeypatch.setenv("AUTOPHONE_FOREGROUND_ENABLED", "0")
        reset_control_plane_cache()
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/api/platform/targets")
            assert resp.status_code == 503
            detail = resp.json()["detail"]
            assert detail["code"] == "control_plane_unavailable"
            assert "autophone[postgres]" in detail["message"]

        # 8. CLI control command fails closed with exit code 2 and actionable JSON detail.
        import autophone.cli

        capsys.readouterr()
        rc = autophone.cli.main(["control", "rig", "list"])
        assert rc == 2
        payload = json.loads(capsys.readouterr().err)
        assert payload["refused"] == "control_plane_unavailable"
        assert "autophone[postgres]" in payload["detail"]
    finally:
        reset_control_plane_cache()


def test_psycopg_error_type_helpers_track_driver_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    assert psycopg.Error in psycopg_error_types()
    assert psycopg.errors.UniqueViolation in psycopg_errors_by_name("UniqueViolation")
    assert set(psycopg_errors_by_name("DeadlockDetected", "SerializationFailure", "LockNotAvailable")) == {
        psycopg.errors.DeadlockDetected,
        psycopg.errors.SerializationFailure,
        psycopg.errors.LockNotAvailable,
    }

    _block_psycopg(monkeypatch)
    assert psycopg_error_types() == ()
    assert psycopg_errors_by_name("UniqueViolation", "DeadlockDetected") == ()
