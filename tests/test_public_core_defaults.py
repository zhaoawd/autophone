"""The public core requires callers to select their own app and case library."""
from importlib import import_module
import inspect
import json
from pathlib import Path

import pytest

from pkgs.ownership import owner


def test_core_build_excludes_non_core_extensions_and_retains_relocated_rig_tools():
    assert owner("autophone/device/app_uninstall_helper.py") == "delivery"
    assert owner("autophone/adapters/http/stores.py") == "delivery"
    assert owner("autophone/workflow/example.py") == "delivery"
    assert owner("autophone/rig/emit_target.py") == "rig"
    assert owner("tools/phase1/usb_diagnosis.py") == "rig"


def test_case_inputs_have_no_example_default():
    from autophone.execution.engine.runner import run
    from autophone.observation.coverage.generate import generate_coverage

    for function in (run, generate_coverage):
        assert inspect.signature(function).parameters["cases_path"].default is inspect.Parameter.empty


def test_fixture_requires_app_and_preserves_explicit_app(tmp_path):
    from autophone.execution.fixtures.contract import (
        FixtureContractError,
        default_fixture_spec,
        load_fixture_spec,
    )

    selected_app = "com.example.app"
    fixture = default_fixture_spec(app=selected_app, case_id="case-1")
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture.to_dict()))
    assert load_fixture_spec(path).app == selected_app

    data = fixture.to_dict()
    data.pop("app")
    path.write_text(json.dumps(data))
    with pytest.raises(FixtureContractError, match="app is required"):
        load_fixture_spec(path)


@pytest.mark.parametrize("module", [
    "autophone.observation.frida.net_capture",
    "autophone.observation.frida.objc_bundle_probe",
    "autophone.observation.frida.run_with_capture",
])
def test_missing_app_probe_target_is_rejected_before_device_access(monkeypatch, module):
    monkeypatch.delenv("AP_BUNDLE_ID", raising=False)
    probe = import_module(module)
    with pytest.raises(SystemExit, match="AP_BUNDLE_ID"):
        probe.main()


def test_app_location_probe_uses_explicit_bundle_without_store_default(monkeypatch):
    from autophone.location.adapters.corelocation_control import parse_args

    for key in ("AP_BUNDLE_ID", "AP_STORE_LAT", "AP_STORE_LNG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("sys.argv", ["probe", "--latitude", "1", "--longitude", "2"])
    with pytest.raises(SystemExit) as missing:
        parse_args()
    assert missing.value.code == 2

    monkeypatch.setattr("sys.argv", ["probe", "--latitude", "1", "--longitude", "2",
                                   "--bundle-id", "com.example.app"])
    args = parse_args()
    assert args.bundle_id == "com.example.app"
    assert args.store_latitude is args.store_longitude is None


def test_optional_example_stays_out_of_runtime_default():
    from autophone.location.points import load_points

    assert not load_points(
        seed_path=Path("autophone/_data/location-points.yaml"),
        overlay_path=Path("/nonexistent-location-overlay"),
    )


def test_contract_probe_without_app_fails_before_frida(monkeypatch):
    from autophone.device.frida_contract_probe import probe

    monkeypatch.delenv("AP_APP_NAME", raising=False)
    assert "AP_APP_NAME" in probe()["error"]
