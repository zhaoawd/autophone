"""Public entry-point contracts without a guest or an external service."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

import httpx2
import pytest

from autophone.assets.apps import create_app_profile
from autophone.adapters.cli.location import dashboard_url, location_request
from autophone.adapters.cli import rig
from autophone.rig import region_probe
from autophone.device import uds
from autophone.device import springboard_residue as residue
from autophone.device.target import VPhoneTarget, target_scope
from autophone.packages import store
from autophone.record.lift import LiftError, recorded_steps
from tests.test_rig_emit_target import _env
from autophone.rig.emit_target import target_from_environment


@pytest.mark.parametrize(("output", "verdict"), [
    ("done 42 15\n", "clean"),
    ("tag255 0000000100000000 0000000100004000 r-x/rwx PRV\ndone 42 15\n", "residue"),
    ("done 41 15\n", "check_failed"),
    ("tag255 0000000100000000 0000000100004000 r-x/rwx PRV\n", "check_failed"),
    ("unrecognized output\ndone 42 15\n", "check_failed"),
])
def test_springboard_probe_preserves_installation_admission(monkeypatch, output, verdict):
    monkeypatch.delenv(residue.REGION_PROBE_ENV, raising=False)
    calls = []

    def shell(command):
        calls.append(command)
        if command.startswith("LC_ALL=C"):
            return 0, "42 Thu Nov 19 12:00:00 2026 /System/Library/CoreServices/SpringBoard.app/SpringBoard\n"
        if command == f"{residue.PROCEXP} 42 regions":
            return 127, f"{residue.PROCEXP}: not found"
        assert command == f"{residue.REGION_PROBE} 42"
        return 0, output

    result = residue.inspect_springboard_residue(shell)
    assert result["verdict"] == verdict
    assert result["command"] == f"{residue.REGION_PROBE} 42"
    if verdict != "check_failed":
        assert result["regions_parsed"] == 15
        assert calls[0] == calls[-1]


def test_region_probe_staging_and_cleanup_use_only_the_selected_socket(tmp_path, monkeypatch):
    probe = tmp_path / "autophone-springboard-regions"
    probe.write_bytes(b"fixture")
    target = SimpleNamespace(socket=tmp_path / "B.sock", protocol="upstream_v1", rig_id="B")
    reports = []
    cleanups = []

    def request(message, **options):
        reports.append((message, options))
        return {"ok": True}, None

    def cleanup(command, **options):
        cleanups.append((command, options))
        return SimpleNamespace(code=0)

    monkeypatch.setattr(region_probe, "request", request)
    monkeypatch.setattr(region_probe, "run_shell", cleanup)
    with ExitStack() as stack:
        remote = region_probe.stage_region_probe(stack, target, probe)[residue.REGION_PROBE_ENV]
        assert reports[0][0] == {"t": "file_put", "load": str(probe), "path": remote, "perm": "700", "screen": False}
        assert reports[0][1]["sock_path"] == str(target.socket)
        assert not cleanups
    assert cleanups == [(f"rm -f {remote}", {
        "sock_path": str(target.socket), "protocol": "upstream_v1", "timeout_ms": 5000,
    })]


def test_dashboard_ipa_registration_with_an_independent_app_asset_root(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AUTOPHONE_", "VPHONE_"))}
    env.update(AUTOPHONE_APP_ASSET_DIR=str(tmp_path / "assets"),
               AUTOPHONE_STATE_DIR=str(tmp_path / "state"), AUTOPHONE_SCHEDULER_ENABLED="0")
    script = """
import hashlib
from io import BytesIO
import plistlib
from zipfile import ZipFile

from fastapi.testclient import TestClient
from autophone.adapters.http.app import app

def ipa(bundle):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("Payload/Example.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": bundle,
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
        }))
    return buffer.getvalue()

with TestClient(app) as client:
    profile = client.post("/api/apps", json={
        "app": "example", "bundle_id": "com.example.app", "display_name": "Example",
    })
    assert profile.is_success, profile.text
    endpoint = "/api/apps/example/packages"
    for bundle, expected_status in (("com.example.other", 422), ("com.example.app", 200)):
        data = ipa(bundle)
        upload = client.post(endpoint + "/upload?filename=example.ipa", content=data)
        assert upload.is_success, upload.text
        register = client.post(endpoint, json={"ipa_path": upload.json()["ipa_path"]})
        assert register.status_code == expected_status, register.text
    registered = client.get(endpoint).json()
    entry, = registered["packages"]
    assert entry["build_id"]
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
"""
    process = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                             text=True, timeout=25)
    assert process.returncode == 0, process.stdout + process.stderr
    assert (tmp_path / "assets/apps/example/profile.yaml").is_file()
    assert (tmp_path / "state/packages/example/manifest.json").is_file()


def test_upstream_target_uses_bundle_without_checkout(tmp_path):
    env = _env(tmp_path)
    env.pop("VPHONE_CLI_DIR")
    bundle = tmp_path / "VPhone.bundle"
    env.update(AUTOPHONE_VPHONE_PROTOCOL="upstream_v1", AUTOPHONE_CORE_BUNDLE=str(bundle))
    target = target_from_environment(env)
    assert target.cli_dir == bundle / "Contents" / "MacOS"


def test_location_client_uses_pinned_device_and_refuses_foreign_sessions():
    requests = []

    def dispatch(request):
        requests.append(request)
        if request.method == "GET" and request.url.path.startswith("/api/location/sessions"):
            return httpx2.Response(200, json={"spec": {"device": "B"}})
        return httpx2.Response(200, json={"ok": True})

    with httpx2.Client(base_url="http://127.0.0.1:8000", transport=httpx2.MockTransport(dispatch)) as client:
        assert location_request(argparse.Namespace(action="current", rig="B"), client)["ok"]
        args = argparse.Namespace(action="set", rig="A", owner="test", session_id="sample",
                                  latitude=1, longitude=2)
        assert location_request(args, client)["ok"]
        change = json.loads(requests[1].content)
        assert change["spec"]["device"] == "A"
        assert change["spec"]["targets"][0]["target_id"] == "system:A"
        assert change["initial_sample"]["coordinate"]["latitude"] == 1
        args.action = "route"
        args.route_id = "demo-route"
        location_request(args, client)
        assert json.loads(requests[2].content)["spec"]["source"]["route_id"] == "demo-route"
        args.action = "stop"
        with pytest.raises(ValueError, match="different Rig"):
            location_request(args, client)
        assert len(requests) == 4
        args.rig = "B"
        location_request(args, client)
        assert requests[-1].method == "DELETE"
        assert json.loads(requests[-1].content) == {"owner": "test"}


def test_location_rejects_remote_cleartext():
    with pytest.raises(ValueError, match="HTTPS"):
        dashboard_url("http://dashboard.example:8000")


def test_recorded_recipe_requires_guest_ack_and_keeps_order():
    tap = {"kind": "tap", "preview": [28, 41], "uds_result": {"ok": True}}
    assert recorded_steps({"events": [tap, {"kind": "key", "name": "home", "uds_result": {"ok": True}}]}) == [
        {"action": "tap_xy", "px": 28, "py": 41},
        {"action": "press_key", "name": "home"},
    ]
    with pytest.raises(LiftError, match="回执"):
        recorded_steps({"events": [{**tap, "uds_result": {"ok": False}}]})


def test_rig_stack_releases_started_vm_on_second_start_failure(monkeypatch, tmp_path):
    events = []
    one, two = SimpleNamespace(rig_id="A"), SimpleNamespace(rig_id="B")

    class Lifecycle:
        def __init__(self, target):
            self.target = target

        def status(self):
            return SimpleNamespace(affected_pids=())

        def start(self, *, allow_multi_vm, timeout_s, headless):
            assert headless
            events.append(("start", self.target.rig_id, allow_multi_vm))
            if self.target is two:
                raise RuntimeError("fixture start failure")

        def stop(self):
            events.append(("stop", self.target.rig_id))

    monkeypatch.setenv("AUTOPHONE_DATABASE_URL", "dbname=invalid")
    monkeypatch.setattr(rig, "configured_targets", lambda paths: [one, two])
    monkeypatch.setattr(rig, "_require_free_ports", lambda *ports: None)
    monkeypatch.setattr(rig, "_wait_until", lambda *args: None)
    monkeypatch.setattr(rig, "RigLifecycle", Lifecycle)
    monkeypatch.setattr(rig.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(rig, "_stop_child", lambda process: events.append(("stop", "control")))
    with pytest.raises(RuntimeError, match="fixture start failure"):
        rig.run_stack([tmp_path / "A", tmp_path / "B"], 8100, 8000)
    assert events == [("start", "A", False), ("start", "B", True), ("stop", "A"), ("stop", "control")]


def test_rig_stack_registers_installation_on_each_agent(monkeypatch, tmp_path):
    paths = [tmp_path / "A.json", tmp_path / "B.json"]
    launched = []

    def start(args, **_options):
        launched.append(args)
        return SimpleNamespace(poll=lambda: None)

    def wait(_processes, _ready, label):
        if label == "dashboard and all selected Rigs":
            raise KeyboardInterrupt

    monkeypatch.setenv("AUTOPHONE_DATABASE_URL", "dbname=fixture")
    monkeypatch.setattr(rig, "configured_targets", lambda _paths: [
        SimpleNamespace(rig_id="A"), SimpleNamespace(rig_id="B"),
    ])
    monkeypatch.setattr(rig, "_require_free_ports", lambda *_ports: None)
    monkeypatch.setattr(rig, "_wait_until", wait)
    monkeypatch.setattr(rig, "RigLifecycle", lambda _target: SimpleNamespace(
        status=lambda: SimpleNamespace(affected_pids=()),
        start=lambda **_options: None,
        stop=lambda: None,
    ))
    monkeypatch.setattr(rig.subprocess, "Popen", start)
    monkeypatch.setattr(rig, "_stop_child", lambda _child: None)
    with pytest.raises(KeyboardInterrupt):
        rig.run_stack(paths, 8100, 8000)
    assert [args[3:] for args in launched if args[2] == "autophone.rig.main"] == [
        ["--target", str(path), "--capability", "vm_uds", "--capability", "app_install"]
        for path in paths
    ]


@pytest.mark.parametrize("transfer_ok", [True, False])
def test_upstream_install_uses_selected_uds_and_never_ssh(tmp_path, monkeypatch, transfer_ok):
    bundle_id = "com.example.app"
    create_app_profile("example", bundle_id, "Example", root=tmp_path)
    ipa = tmp_path / "example.ipa"
    with zipfile.ZipFile(ipa, "w") as archive:
        archive.writestr("Payload/Example.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
        }))
    entry = store.add_package("example", ipa, root=tmp_path)
    vm = tmp_path / "selected"
    target = VPhoneTarget(cli_dir=tmp_path, vm_dir=vm, sock=vm / "vphone.sock",
                          device="selected", protocol="upstream_v1")
    calls = []
    uploaded = []

    def exchange(message, sock_path, timeout, mode, **_options):
        assert (sock_path, mode) == (str(target.sock), "upstream_v1")
        kind = message["t"]
        result = {}
        if kind == "capabilities":
            result.update(protocol=mode, guest_connected=True, guest_capabilities=["ipa_install"],
                          commands={"file_put": True, "rpc": True, "shell": True, "app_list": True})
        elif kind == "push":
            assert Path(message["local"]).read_bytes() == ipa.read_bytes()
            uploaded.append(message["path"])
            result.update(path=message["path"], size=ipa.stat().st_size)
        elif message.get("method") == "apps.install":
            assert transfer_ok and message["params"]["path"] == uploaded[0]
            result["bundle_id"] = bundle_id
        elif message.get("method") == "apps.list":
            result["apps"] = [{"bundle_id": bundle_id, "path": "/var/containers/Example.app"}]
        else:
            assert message["method"] == "shell.exec"
            cmd = message["params"]["cmd"].split("; ", 1)[-1]
            calls.append(shlex.split(cmd))
            assert not cmd.startswith("uicache")
            result.update(stdout="",
                          stderr="", timed_out=False, exit_code=0,
                          stdout_truncated=False, stderr_truncated=False)
        now = time.perf_counter()
        response = {"ok": True, **result} if kind == "capabilities" else {"ok": True, "result": result}
        if kind == "push" and not transfer_ok:
            response = {"ok": False, "error": "fixture transfer refused"}
        return response, (now, now, now, now), b""

    monkeypatch.setattr(uds, "_exchange", exchange)
    monkeypatch.setattr(store, "scp_to_guest", lambda *_args: pytest.fail("unexpected SSH"))
    with target_scope(target):
        if transfer_ok:
            result = store.install_package("example", entry.slot, root=tmp_path)
            assert result["verified"] and result["build_id"] == entry.build_id
            assert store.load_target_receipt("example", root=tmp_path) is not None
        else:
            with pytest.raises(store.TransferError, match="fixture transfer refused"):
                store.install_package("example", entry.slot, root=tmp_path)
            assert store.load_target_receipt("example", root=tmp_path) is None
    assert [call for call in calls if call[:2] == ["rm", "-f"]] == [["rm", "-f", uploaded[0]]]


def test_rig_init_derives_udid_and_allocates_distinct_ports(tmp_path, monkeypatch):
    bundle = tmp_path / "VPhone.bundle"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    lib_root = tmp_path / "machines"
    monkeypatch.setenv("VPHONE_LIBRARY_ROOT", str(lib_root))

    for rig_id, udid in (("rig-a", "00008101-000A11112222333A"), ("rig-b", "00008101-000B11112222333B")):
        vm = lib_root / rig_id
        vm.mkdir(parents=True)
        (vm / "udid-prediction.txt").write_text(f"UDID: {udid}\n", encoding="utf-8")

    target_a, path_a = rig.init_target("rig-a", bundle=bundle, host_id="mac-host")
    target_b, path_b = rig.init_target("rig-b", bundle=bundle, host_id="mac-host")

    assert path_a == (lib_root / "rig-a" / "autophone" / "target.json").resolve()
    assert target_a.expected_usb_udid == "00008101-000A11112222333A"
    assert target_a.ssh_port == 2222 and target_a.frida_port == 27042
    assert path_b.is_file()
    assert target_b.expected_usb_udid == "00008101-000B11112222333B"
    assert target_b.ssh_port == 2223 and target_b.frida_port == 27043
    # Re-initializing rig-a preserves its own ports without colliding with itself.
    target_a2, _ = rig.init_target("rig-a", bundle=bundle, host_id="mac-host")
    assert target_a2.ssh_port == 2222 and target_a2.frida_port == 27042


def test_rig_up_single_target_runs_local_mode_without_database(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOPHONE_DATABASE_URL", raising=False)
    target = SimpleNamespace(
        rig_id="rig-a",
        protocol="upstream_v1",
        core_bundle=tmp_path / "VPhone.bundle",
        vm_dir=tmp_path / "machines" / "rig-a",
        socket=tmp_path / "machines" / "rig-a" / "vphone.sock",
    )
    events = []
    launched = []

    class Lifecycle:
        def __init__(self, item):
            self.item = item

        def status(self):
            return SimpleNamespace(affected_pids=())

        def start(self, **kwargs):
            events.append(("vm_start", self.item.rig_id, kwargs["headless"]))

        def stop(self):
            events.append(("vm_stop", self.item.rig_id))

    def fake_popen(args, **kwargs):
        launched.append((args, kwargs["env"]))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(rig, "configured_targets", lambda _paths: [target])
    monkeypatch.setattr(rig, "_require_free_ports", lambda *_ports: None)
    monkeypatch.setattr(rig, "RigLifecycle", Lifecycle)
    monkeypatch.setattr(rig.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rig, "_stop_child", lambda _proc: events.append(("dashboard_stop",)))
    monkeypatch.setattr(rig, "_wait_until", lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        rig.run_stack([tmp_path / "rig-a.json"], 8100, 8000)

    assert events == [("vm_start", "rig-a", True), ("dashboard_stop",), ("vm_stop", "rig-a")]
    assert len(launched) == 1
    cmd, child_env = launched[0]
    assert "--no-control-plane" in cmd and "serve" in cmd
    assert child_env["AUTOPHONE_VPHONE_PROTOCOL"] == "upstream_v1"
    assert child_env["AP_DEVICE"] == "rig-a"
    assert "AUTOPHONE_DATABASE_URL" not in child_env


def test_rig_up_multi_target_without_database_guides_ephemeral_db(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOPHONE_DATABASE_URL", raising=False)
    monkeypatch.setattr(rig, "configured_targets", lambda _paths: [
        SimpleNamespace(rig_id="A"), SimpleNamespace(rig_id="B"),
    ])
    with pytest.raises(ValueError, match="--ephemeral-db"):
        rig.run_stack([tmp_path / "A.json", tmp_path / "B.json"], 8100, 8000)


def test_cli_help_hides_experimental_subcommands_by_default(monkeypatch):
    from autophone.cli import EXPERIMENTAL_CLI_COMMANDS, build_parser

    monkeypatch.delenv("AUTOPHONE_CLI_SHOW_EXPERIMENTAL", raising=False)
    default_help = build_parser(include_extensions=False).format_help()
    assert "rig" in default_help and "location" in default_help and "doctor" in default_help
    for hidden in ("telemetry", "acceptance", "knowledge"):
        assert hidden in EXPERIMENTAL_CLI_COMMANDS
        assert f"\n    {hidden}" not in default_help

    monkeypatch.setenv("AUTOPHONE_CLI_SHOW_EXPERIMENTAL", "1")
    expanded_help = build_parser(include_extensions=False).format_help()
    assert "telemetry" in expanded_help and "acceptance" in expanded_help


def test_doctor_reports_nonblocking_springboard_probe_advisory(monkeypatch, tmp_path):
    from autophone.adapters.cli.doctor import _format_human_summary
    from autophone.application.diagnostics import DiagnosticRequest, DiagnosticsApplication

    monkeypatch.delenv("AUTOPHONE_SPRINGBOARD_REGION_PROBE", raising=False)
    monkeypatch.setattr("autophone.paths.CODE_ROOT", tmp_path)
    monkeypatch.setattr("autophone.execution.engine.runner.gate", lambda sock, timeout=8.0: (True, "ok"))
    payload = DiagnosticsApplication().basic(DiagnosticRequest(app="example", device="rig-a", sock="/tmp/fake.sock"))
    assert payload["status"] == "healthy"
    assert payload["capabilities"]["system_ui_and_location_ready"] is True
    assert payload["capabilities"]["springboard_region_probe"]["required_for"] == "third_party_ipa_install"
    summary = _format_human_summary(payload)
    assert "[OK] 系统界面与定位就绪" in summary
    assert "build_guest_helpers.sh" in summary


def test_ios_settings_smoke_example_lints_and_runs_synthetic_ocr(tmp_path):
    from unittest.mock import patch
    from autophone.assets.apps import load_app_profile
    from autophone.contracts import validate_or_raise
    from autophone.execution.engine.actions import CaseContext
    from autophone.execution.engine.recipe import load_recipe, run_recipe
    import yaml

    example_root = Path(__file__).resolve().parents[1] / "examples" / "ios-settings-smoke"
    profile = load_app_profile("ios_settings_smoke", root=example_root, state_root=tmp_path)
    assert profile.bundle_id == "com.apple.Preferences"
    ops_doc = yaml.safe_load(profile.operations_path().read_text(encoding="utf-8"))
    validate_or_raise("operation.v1.json", ops_doc)

    r1_steps = load_recipe(profile.recipes_dir() / "r1.yaml")
    r2_steps = load_recipe(profile.recipes_dir() / "r2.yaml")
    ctx = CaseContext("R1", tmp_path, sock_path=str(tmp_path / "absent.sock"))
    observation = {"image_w": 100, "image_h": 200, "boxes": [
        {"text": "General", "confidence": 1.0, "x": 10, "y": 20, "w": 20, "h": 10},
    ]}
    with patch.object(ctx, "ocr_now", return_value=observation):
        assert run_recipe(r1_steps, ctx)[0] == "pass"
        assert run_recipe(r2_steps, ctx)[0] == "fail"


def test_dismiss_usb_trust_prompt_decline_position():
    from tools.phase1.dismiss_usb_trust_prompt import decline_position

    ocr = {
        "image_w": 430,
        "image_h": 932,
        "boxes": [
            {"text": "Trust This Computer?", "x": 90, "y": 400, "w": 250, "h": 24},
            {"text": "Don’t Trust", "x": 70, "y": 540, "w": 110, "h": 30},
            {"text": "Trust", "x": 250, "y": 540, "w": 90, "h": 30},
        ],
    }
    assert decline_position(ocr) == (125, 555)


def test_dashboard_device_sessions_adapter_roundtrip():
    import base64
    from contextlib import contextmanager
    import json
    from tools.phase1.dismiss_usb_trust_prompt import DashboardDeviceSessions

    seen = []

    class FakeResponse:
        def __init__(self, body: bytes, headers: dict[str, str]):
            self._body = body
            self.headers = headers

        def read(self) -> bytes:
            return self._body

    @contextmanager
    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.full_url.endswith("/console"):
            yield FakeResponse(
                json.dumps({
                    "session_id": "ctl-9",
                    "access_token": "tok-secret",
                    "access_header": "x-console-access-token",
                }).encode("utf-8"),
                {},
            )
        elif req.full_url.endswith("/frame"):
            yield FakeResponse(b"jpeg-bytes", {"X-Frame-Seq": "4", "X-Captured-At": "123.5"})
        elif req.full_url.endswith("/event"):
            yield FakeResponse(json.dumps({"ok": True, "seq": 5}).encode("utf-8"), {})
        elif req.full_url.endswith("/stop"):
            yield FakeResponse(json.dumps({"lifecycle": "closed", "error_reason": None}).encode("utf-8"), {})
        else:
            raise AssertionError(f"unexpected URL {req.full_url}")

    sessions = DashboardDeviceSessions("http://127.0.0.1:8000", auth=("ops", "pw"), opener=opener)
    started = sessions.call("rig-a", "console_start", {"request_id": "req-1"})
    assert started["session_id"] == "ctl-9"
    assert started["controller_token"] == "tok-secret"

    frame = sessions.call("rig-a", "console_frame", {"session_id": "ctl-9", "controller_token": "tok-secret"})
    assert frame["seq"] == 4
    assert frame["captured_at"] == 123.5
    assert base64.b64decode(frame["jpeg_b64"]) == b"jpeg-bytes"

    receipt = sessions.call(
        "rig-a",
        "console_event",
        {"session_id": "ctl-9", "controller_token": "tok-secret", "event": {"kind": "key", "name": "home"}},
    )
    assert receipt["ok"] is True

    closed = sessions.call("rig-a", "console_stop", {"session_id": "ctl-9", "controller_token": "tok-secret"})
    assert closed["lifecycle"] == "closed"
    assert seen[0][2]["Authorization"].startswith("Basic ")
    assert seen[1][2]["X-console-access-token"] == "tok-secret"
    assert json.loads(seen[2][3].decode("utf-8")) == {"kind": "key", "name": "home"}


def test_ensure_usb_trust_checked_is_idempotent_per_boot_uuid(tmp_path):
    from tools.phase1.dismiss_usb_trust_prompt import ensure_usb_trust_checked, read_guest_boot_uuid

    target = SimpleNamespace(
        rig_id="rig-a",
        socket=tmp_path / "vphone.sock",
        protocol="upstream_v1",
        runtime_dir=tmp_path / "runtime",
    )
    boot_a = "11111111-2222-3333-4444-555555555555"
    boot_b = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert read_guest_boot_uuid(
        target, shell_runner=lambda *_a, **_k: SimpleNamespace(code=0, stdout=boot_a + "\n", stderr=""),
    ) == boot_a
    with pytest.raises(RuntimeError, match="invalid guest boot UUID"):
        read_guest_boot_uuid(
            target, shell_runner=lambda *_a, **_k: SimpleNamespace(code=0, stdout="not-a-uuid", stderr=""),
        )

    calls = []

    def fake_dismiss(_client, rig_id, evidence_dir, *, wait_s):
        calls.append((rig_id, evidence_dir, wait_s))
        evidence_dir.mkdir(parents=True, exist_ok=False)
        return {"status": "not_observed", "frames": 2}

    first = ensure_usb_trust_checked(
        target,
        client=object(),
        wait_s=5.0,
        read_boot_uuid_fn=lambda _t: boot_a,
        dismiss_fn=fake_dismiss,
    )
    assert first["status"] == "not_observed"
    assert first["boot_uuid"] == boot_a
    assert len(calls) == 1

    second = ensure_usb_trust_checked(
        target,
        client=object(),
        wait_s=5.0,
        read_boot_uuid_fn=lambda _t: boot_a,
        dismiss_fn=fake_dismiss,
    )
    assert second["status"] == "already_checked"
    assert len(calls) == 1

    third = ensure_usb_trust_checked(
        target,
        client=object(),
        wait_s=5.0,
        read_boot_uuid_fn=lambda _t: boot_b,
        dismiss_fn=fake_dismiss,
    )
    assert third["status"] == "not_observed"
    assert len(calls) == 2


def test_rig_up_local_mode_invokes_usb_trust_check_when_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOPHONE_DATABASE_URL", raising=False)
    runtime_dir = tmp_path / "machines" / "rig-a" / "autophone"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "decline-usb-trust").write_text("", encoding="utf-8")
    target = SimpleNamespace(
        rig_id="rig-a",
        protocol="upstream_v1",
        core_bundle=tmp_path / "VPhone.bundle",
        vm_dir=tmp_path / "machines" / "rig-a",
        socket=tmp_path / "machines" / "rig-a" / "vphone.sock",
        runtime_dir=runtime_dir,
    )
    checked = []

    class Lifecycle:
        def __init__(self, _item):
            pass

        def status(self):
            return SimpleNamespace(affected_pids=())

        def start(self, **_kwargs):
            pass

        def stop(self):
            pass

    waits = iter([None, KeyboardInterrupt()])

    def fake_wait_until(*_a, **_k):
        nxt = next(waits)
        if isinstance(nxt, BaseException):
            raise nxt

    monkeypatch.setattr(rig, "configured_targets", lambda _paths: [target])
    monkeypatch.setattr(rig, "_require_free_ports", lambda *_ports: None)
    monkeypatch.setattr(rig, "RigLifecycle", Lifecycle)
    monkeypatch.setattr(rig.subprocess, "Popen", lambda *_a, **_k: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(rig, "_stop_child", lambda _proc: None)
    monkeypatch.setattr(rig, "_wait_until", fake_wait_until)
    monkeypatch.setattr(
        rig,
        "_run_usb_trust_check",
        lambda tgt, **kw: checked.append((tgt.rig_id, kw)) or {"status": "not_observed"},
    )

    with pytest.raises(KeyboardInterrupt):
        rig.run_stack([tmp_path / "rig-a.json"], 8100, 8000, usb_trust_wait=12.0)

    assert len(checked) == 1
    assert checked[0][0] == "rig-a"
    assert checked[0][1]["dashboard_http"] == "http://127.0.0.1:8000"
    assert checked[0][1]["wait_s"] == 12.0

    with pytest.raises(ValueError, match="--usb-trust-wait"):
        rig.run_stack([tmp_path / "rig-a.json"], 8100, 8000, usb_trust_wait=99.0)


def test_usb_trust_console_warmup_retries_transient_503_and_fails_closed_on_409():
    from autophone.application.device_session import DeviceSessionRefused
    from tools.phase1.dismiss_usb_trust_prompt import _start_console_with_warmup

    attempts = {"n": 0}

    class WarmupClient:
        def call(self, rig_id, op, payload):
            assert rig_id == "rig-a" and op == "console_start"
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise DeviceSessionRefused("no frame yet", status=503, code="device_error")
            return {"session_id": "ctl-1", "controller_token": "tok"}

    started = _start_console_with_warmup(WarmupClient(), "rig-a", warmup_s=5.0, sleep_fn=lambda _: None)
    assert started["session_id"] == "ctl-1"
    assert attempts["n"] == 3

    class BusyClient:
        def call(self, _rig_id, _op, _payload):
            raise DeviceSessionRefused("busy", status=409, code="device_busy")

    with pytest.raises(DeviceSessionRefused, match="busy"):
        _start_console_with_warmup(BusyClient(), "rig-a", warmup_s=5.0, sleep_fn=lambda _: None)


def test_dismiss_retries_transient_503_on_readonly_console_frame(tmp_path, monkeypatch):
    import base64
    from autophone.application.device_session import DeviceSessionRefused
    from tools.phase1 import dismiss_usb_trust_prompt as mod

    state = {"frames": 0, "seq": 0, "stopped": False}
    jpg = base64.b64encode(b"\xff\xd8\xff\xd9").decode("ascii")

    class FlakyFrameClient:
        def call(self, _rig, op, _payload):
            if op == "console_start":
                return {"session_id": "ctl-1", "controller_token": "tok"}
            if op == "console_frame":
                state["frames"] += 1
                if state["frames"] == 2:
                    raise DeviceSessionRefused("display geometry unavailable", status=503, code="device_error")
                state["seq"] += 1
                return {"seq": state["seq"], "captured_at": 1.0, "jpeg_b64": jpg}
            if op == "console_event":
                return {"ok": True}
            if op == "console_stop":
                state["stopped"] = True
                return {"lifecycle": "closed", "error_reason": None}
            raise AssertionError(op)

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        mod, "run_ocr", lambda _p: {"image_w": 430, "image_h": 932, "boxes": [{"text": "Settings", "x": 10, "y": 10, "w": 50, "h": 20}]},
    )
    out = tmp_path / "evidence"
    result = mod.dismiss(FlakyFrameClient(), "rig-a", out, wait_s=0.0, console_warmup_s=1.0, frame_retry_s=5.0)
    assert result["status"] == "not_observed"
    assert state["stopped"] is True
    assert state["frames"] >= 4


def test_local_processes_ignores_unrelated_vphone_cli_path_arguments(monkeypatch):
    from autophone.rig import lifecycle as lc

    fake_ps = (
        "101 1 /Applications/Xcode.app/usr/bin/swift-frontend -c /tmp/build/vphone-cli/Sources/Main.swift\n"
        "202 1 /Library/Application Support/vphone/VPhone.bundle/Contents/MacOS/vphone-vm --config /tmp/a/config.plist\n"
    )
    inspected: list[int] = []
    monkeypatch.setattr(
        lc.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(["ps"], 0, stdout=fake_ps, stderr=""),
    )
    monkeypatch.setattr(
        lc,
        "process_arguments",
        lambda pid: inspected.append(pid) or [
            "/Library/Application Support/vphone/VPhone.bundle/Contents/MacOS/vphone-vm",
            "--config",
            "/tmp/a/config.plist",
        ],
    )
    rows = lc._local_processes()
    assert inspected == [202]
    assert len(rows) == 2
    assert "vphone-vm" in rows[1].command
