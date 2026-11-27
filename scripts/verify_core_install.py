"""Exercise an installed public core outside its checkout, using synthetic OCR input."""
import argparse
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch


_OPTIONAL_POSTGRES_PACKAGES = frozenset({"psycopg", "psycopg-binary", "psycopg-c", "psycopg-pool"})
_EXTRA_MARKER_RE = re.compile(r"\bextra\s*==")
_POSTGRES_EXTRA_MARKER_RE = re.compile(r"""\bextra\s*==\s*['"]postgres['"]""")


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _requirement_name(requirement: str) -> str:
    head = requirement.split(";", 1)[0].strip()
    raw = re.split(r"[\s\[(<>=!~@]", head, maxsplit=1)[0]
    return _normalize_dist_name(raw)


def verify_optional_postgres(*, installed_core: bool) -> None:
    import importlib.metadata

    for dist_name in ("autophone", "autophone-kernel", "autophone-rig", "autophone-domain", "autophone-console"):
        reqs = importlib.metadata.requires(dist_name) or []
        for req in reqs:
            _, _, marker = req.partition(";")
            if not _EXTRA_MARKER_RE.search(marker) and _requirement_name(req) in _OPTIONAL_POSTGRES_PACKAGES:
                raise AssertionError(f"{dist_name} base dependency unexpectedly requires psycopg: {req!r}")
        if dist_name in {"autophone", "autophone-rig"}:
            meta = importlib.metadata.metadata(dist_name)
            extras = {_normalize_dist_name(item) for item in (meta.get_all("Provides-Extra") or [])}
            assert "postgres" in extras, f"{dist_name} missing Provides-Extra: postgres ({sorted(extras)})"
            assert any(
                _requirement_name(req) in _OPTIONAL_POSTGRES_PACKAGES
                and _POSTGRES_EXTRA_MARKER_RE.search(req.partition(";")[2])
                for req in reqs
            ), f"{dist_name} missing psycopg requirement under [postgres] extra"

    if not installed_core:
        return

    assert find_spec("psycopg") is None, "psycopg must not be installed in a base core install"
    assert find_spec("psycopg_binary") is None, "psycopg-binary must not be installed in a base core install"

    from autophone.application.control_plane import (
        ControlPlaneUnavailable,
        configured_control_plane,
        reset_control_plane_cache,
    )
    from autophone.control import PostgresControlStore, PostgresDriverUnavailable

    try:
        PostgresControlStore("postgresql://127.0.0.1:5432/autophone")
    except PostgresDriverUnavailable as exc:
        assert "autophone[postgres]" in str(exc), str(exc)
    else:
        raise AssertionError("PostgresControlStore must fail closed when psycopg is absent")

    previous_dsn = os.environ.get("AUTOPHONE_DATABASE_URL")
    os.environ["AUTOPHONE_DATABASE_URL"] = "postgresql://127.0.0.1:5432/autophone"
    reset_control_plane_cache()
    try:
        try:
            configured_control_plane()
        except ControlPlaneUnavailable as exc:
            assert "autophone[postgres]" in str(exc), str(exc)
        else:
            raise AssertionError("configured_control_plane must fail closed when psycopg is absent")
    finally:
        if previous_dsn is None:
            os.environ.pop("AUTOPHONE_DATABASE_URL", None)
        else:
            os.environ["AUTOPHONE_DATABASE_URL"] = previous_dsn
        reset_control_plane_cache()


def verify_runtime_version() -> dict[str, object]:
    import importlib.metadata
    import autophone
    import autophone.version

    assert autophone.__version__ != "0.0.0+unknown"
    identity = autophone.version.build_identity()
    if autophone.version._is_git_checkout():
        assert identity["source"] in {"git", "version.json"}, identity
    else:
        kernel_version = importlib.metadata.version("autophone-kernel")
        assert autophone.__version__ == kernel_version, (autophone.__version__, kernel_version)
        assert identity["source"] == "package_metadata", identity
    return identity


def verify(example: Path) -> None:
    assert find_spec("autophone.workflow") is None
    assert find_spec("autophone.device.app_uninstall_helper") is None
    assert find_spec("autophone.adapters.http.stores") is None
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["AUTOPHONE_STATE_DIR"] = scratch
        os.environ["AUTOPHONE_SCHEDULER_ENABLED"] = "0"
        os.environ.pop("AUTOPHONE_APP_ASSET_DIR", None)
        os.environ.pop("AUTOPHONE_CLI_SHOW_EXPERIMENTAL", None)
        os.environ.pop("AUTOPHONE_DATABASE_URL", None)
        os.environ.pop("AUTOPHONE_CONTROL_HTTP", None)
        os.environ.pop("AUTOPHONE_CONTROL_WS", None)
        verify_runtime_version()
        import autophone.version
        verify_optional_postgres(installed_core=not autophone.version._is_git_checkout())

        from fastapi.testclient import TestClient
        from autophone.adapters.http.app import app
        from autophone.execution.engine.actions import CaseContext
        from autophone.execution.engine.recipe import load_recipe, run_recipe

        cli = Path(sys.executable).with_name("autophone")

        def command(*args):
            return subprocess.check_output([cli, *args], cwd=scratch, text=True)

        assert json.loads(command("app", "list"))["apps"] == []
        help_text = command("--help")
        assert find_spec("autophone.notify") is None
        assert "workflow" not in help_text
        assert "location" in help_text and "rig" in help_text
        assert "acceptance" not in help_text and "failure-sample" not in help_text
        assert "multicase" not in help_text and ",telemetry," not in help_text
        assert "\n    telemetry " not in help_text
        with TestClient(app) as client:
            assert client.get("/health").json()["ok"]
            home = client.get("/").text
            assert "目标 Rig" in home
            assert "用例回归与报告" in home
            assert client.get("/static/platform.js").is_success
            assert client.get("/static/platform-workspace.js").is_success
            assert client.get("/api/platform/targets").json()["mode"] == "local"
            assert client.get("/workspace").status_code == 404
            assert client.get("/api/requirements").status_code == 404
            assert client.get("/api/stores?brand=example").status_code == 404

        os.environ["AUTOPHONE_APP_ASSET_DIR"] = str(example)
        apps_root = example / "apps"
        app_dirs = sorted(p for p in apps_root.iterdir() if p.is_dir() and (p / "profile.yaml").is_file())
        assert app_dirs, f"no example app profiles found under {apps_root}"
        app_name = app_dirs[0].name
        assert json.loads(command("app", "show", app_name))["app"] == app_name
        lint = json.loads(command("app", "case", "lint", app_name))
        assert lint["error_count"] == lint["warning_count"] == 0, lint
        assert "rig_id" in command("rig", "init", "--help")
        assert "symbols" in command("app", "symbols", "--help")

        recipe_path = apps_root / app_name / "recipes" / "r1.yaml"
        steps = load_recipe(recipe_path)
        positive_label = "General" if app_name == "ios_settings_smoke" else "立即下单"
        negative_label = "无关文案" if app_name == "ios_settings_smoke" else "订单记录"
        for label, expected in ((positive_label, "pass"), (negative_label, "fail")):
            ctx = CaseContext("R1", Path(scratch), sock_path=str(Path(scratch) / "absent.sock"))
            observation = {"image_w": 100, "image_h": 200, "boxes": [
                {"text": label, "confidence": 1, "x": 0, "y": 0, "w": 10, "h": 5},
            ]}
            with patch.object(ctx, "ocr_now", return_value=observation):
                status, detail, _ = run_recipe(steps, ctx)
            assert status == expected, detail


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("example", type=Path)
    verify(parser.parse_args().example.resolve())
