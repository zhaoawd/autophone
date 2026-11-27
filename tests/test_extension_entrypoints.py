"""Protocol verification tests for Autophone's public entry points and plugin template.

Keeps `docs/guides/extensions.md`, `docs/guides/extensions.en.md`, and
`examples/plugin-template/` verified directly against the real loaders in
`autophone.rig.plugins`, `autophone.rig.main`, `autophone.control.service`,
`autophone.rig.agent`, and `autophone.rig.device_session`.
"""
from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from queue import Empty, Queue
import tomllib
from typing import Any, Callable, Mapping

import pytest

from autophone.adapters import extensions as console_extensions
from autophone.application.device_session import DEVICE_SESSION_OPS
from autophone.contracts import ContractError, validate_or_raise
from autophone.control.postgres import ControlStateError
from autophone.control.service import ControlPlane
from autophone.device.runtime_probe import RuntimeAdmissionError
from autophone.execution import candidate_lifecycle
from autophone.rig import main as rig_main
from autophone.rig import plugins as rig_plugins
from autophone.rig.agent import RigAgent, StaleDeviceWrite
from autophone.rig.device_session import DeviceSessionAgent
from autophone.rig.target import DeviceTarget

ROOT = Path(__file__).resolve().parents[1]
DOMAIN_PYPROJECT = ROOT / "pkgs" / "autophone-domain" / "pyproject.toml"
TEMPLATE_ROOT = ROOT / "examples" / "plugin-template"
TEMPLATE_PYPROJECT = TEMPLATE_ROOT / "pyproject.toml"
TEMPLATE_SRC = TEMPLATE_ROOT / "src"


class _FakeEntryPoint:
    def __init__(self, name: str, value: str, obj: Any) -> None:
        self.name = name
        self.value = value
        self._obj = obj

    def load(self) -> Any:
        return self._obj


def _make_target(tmp_path: Path) -> DeviceTarget:
    vm_dir = tmp_path / "vm-rig-a"
    vm_dir.mkdir(parents=True, exist_ok=True)
    bundle = tmp_path / "VPhone.bundle"
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    return DeviceTarget(
        host_id="host-test",
        rig_id="rig-a",
        rig_generation=1,
        cli_dir=bundle / "Contents" / "MacOS",
        vm_dir=vm_dir,
        socket=vm_dir / "vphone.sock",
        expected_usb_udid="00008110-TEST000000000001",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        frida_host="127.0.0.1",
        frida_port=27042,
        frida_guest_port=27042,
        log_dir=tmp_path / "logs",
        temp_dir=tmp_path / "tmp",
        package_cache=tmp_path / "packages",
        runtime_dir=tmp_path / "runtime",
        protocol="upstream_v1",
        core_bundle=bundle,
    )


def _load_target_spec(spec: str) -> Any:
    module_name, sep, attr_name = spec.partition(":")
    assert sep == ":" and module_name and attr_name
    return getattr(importlib.import_module(module_name), attr_name)


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    groups: Mapping[str, list[_FakeEntryPoint]],
) -> None:
    def fake_entry_points(*, group: str, name: str | None = None) -> list[_FakeEntryPoint]:
        entries = list(groups.get(group, ()))
        if name is not None:
            return [entry for entry in entries if entry.name == name]
        return entries

    monkeypatch.setattr(module, "entry_points", fake_entry_points)


class _FakeAgentTransport:
    def __init__(
        self,
        *,
        heartbeat_status: str = "running",
        write_valid: bool = True,
    ) -> None:
        self.heartbeat_status = heartbeat_status
        self.write_valid = write_valid
        self.completed: list[tuple[str, int, int, dict[str, Any]]] = []
        self.quarantine_reports: list[dict[str, Any]] = []

    def heartbeat(self, assignment_id: str, attempt_no: int, fencing: int) -> dict[str, Any]:
        return {
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
            "status": self.heartbeat_status,
        }

    def validate_write(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        target: DeviceTarget,
    ) -> dict[str, Any]:
        return {"valid": self.write_valid}

    def complete(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = dict(result)
        self.completed.append((assignment_id, attempt_no, fencing, payload))
        return {
            "assignment_id": assignment_id,
            "status": payload.get("status"),
            "result": payload,
        }

    def report_screen_quarantine(self, state: Mapping[str, Any]) -> dict[str, Any]:
        self.quarantine_reports.append(dict(state))
        return {"ok": True}


class _FakeControlStore:
    evidence_root = None

    def __init__(self) -> None:
        self.submitted: list[tuple[dict[str, Any], str]] = []

    def submit_assignment(self, plan: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        self.submitted.append((dict(plan), idempotency_key))
        return {"assignment_id": "asgn-1", "status": "queued"}


class _FakeSessionSocket:
    def __init__(
        self,
        incoming: list[dict[str, Any]],
        *,
        on_result_count: int,
        on_done: Callable[[], None],
    ) -> None:
        self._incoming: Queue[str] = Queue()
        for message in incoming:
            self._incoming.put(json.dumps(message))
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._on_result_count = on_result_count
        self._on_done = on_done

    def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        results = [item for item in self.sent if item.get("type") == "result"]
        if len(results) >= self._on_result_count:
            self._on_done()

    def recv(self, timeout: float = 1.0) -> str:
        try:
            return self._incoming.get(timeout=min(timeout, 0.05))
        except Empty as exc:
            raise TimeoutError("no scripted message") from exc

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def template_eps(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    monkeypatch.syspath_prepend(str(TEMPLATE_SRC))
    data = tomllib.loads(TEMPLATE_PYPROJECT.read_text(encoding="utf-8"))
    return dict(data["project"]["entry-points"])


# ── 1. Built-in & Template pyproject.toml Parity ───────────────────────────────


def test_builtin_domain_and_template_declare_matching_public_entrypoint_groups(
    template_eps: dict[str, dict[str, str]],
) -> None:
    expected_groups = {
        "autophone.run_admission",
        "autophone.executor",
        "autophone.device_session",
    }
    domain_data = tomllib.loads(DOMAIN_PYPROJECT.read_text(encoding="utf-8"))
    domain_eps = domain_data["project"]["entry-points"]
    assert set(domain_eps) == expected_groups
    assert set(template_eps) == expected_groups

    for source_eps in (domain_eps, template_eps):
        admission_target = next(iter(source_eps["autophone.run_admission"].values()))
        admission_fn = _load_target_spec(admission_target)
        assert callable(admission_fn)
        admission_params = list(inspect.signature(admission_fn).parameters.values())
        assert len(admission_params) >= 1
        assert admission_params[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )

        for group in ("autophone.executor", "autophone.device_session"):
            target_spec = next(iter(source_eps[group].values()))
            factory = rig_main.load_executor_factory(target_spec)
            sig = inspect.signature(factory)
            assert "target" in sig.parameters
            assert "run_root" in sig.parameters


def test_load_executor_factory_rejects_malformed_and_non_callable_specs() -> None:
    for invalid in ("autophone.rig.plugins", ":create_executor", "autophone.rig.plugins:"):
        with pytest.raises(ValueError, match="module:function"):
            rig_main.load_executor_factory(invalid)
    with pytest.raises(TypeError, match="not callable"):
        rig_main.load_executor_factory("autophone.rig.main:DEFAULT_EXECUTOR_FACTORY")


# ── 2. autophone.run_admission Contract ────────────────────────────────────────


def test_run_admission_entrypoint_cardinality_trigger_and_control_plane_boundary(
    monkeypatch: pytest.MonkeyPatch,
    template_eps: dict[str, dict[str, str]],
) -> None:
    target_spec = next(iter(template_eps["autophone.run_admission"].values()))
    validate_fn = _load_target_spec(target_spec)
    ep = _FakeEntryPoint("template_admission", target_spec, validate_fn)

    # Ordinary non-draft regression skips entry-point lookup even with 0 providers.
    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.run_admission": []})
    ordinary_spec = {
        "type": "regression",
        "app": "ios_settings_smoke",
        "case_selector": {"ids": ["R1"], "statuses": ["active"]},
    }
    rig_plugins.validate_run_submission(ordinary_spec)

    # Invalid case_selector fails closed before provider lookup.
    with pytest.raises(ContractError, match="trial_confirmation_selector_invalid"):
        rig_plugins.validate_run_submission({"type": "regression", "case_selector": "bad"})

    draft_spec = {
        "type": "regression",
        "app": "ios_settings_smoke",
        "case_selector": {"ids": ["R1"], "statuses": ["draft"]},
    }

    # 0 providers -> fail closed.
    with pytest.raises(ContractError, match="trial_confirmation_validator_unavailable"):
        rig_plugins.validate_run_submission(draft_spec)

    # 2 providers -> fail closed.
    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.run_admission": [ep, ep]})
    with pytest.raises(ContractError, match="trial_confirmation_validator_unavailable"):
        rig_plugins.validate_run_submission(draft_spec)

    # Exactly 1 provider -> enforces template admission rules.
    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.run_admission": [ep]})
    with pytest.raises(ContractError, match="trial_confirmation_required"):
        rig_plugins.validate_run_submission(draft_spec)

    # Non-draft run carrying trial_confirmation still triggers provider and rejects.
    with pytest.raises(ContractError, match="trial_confirmation_not_applicable"):
        rig_plugins.validate_run_submission({**ordinary_spec, "trial_confirmation": []})

    # ControlPlane translates ContractError to ControlStateError before hitting store.
    store = _FakeControlStore()
    control = ControlPlane(store)  # type: ignore[arg-type]
    with pytest.raises(ControlStateError, match="trial_confirmation_required"):
        control.submit_assignment({"kind": "run", "run_spec": draft_spec}, "idem-reject")
    assert store.submitted == []

    admitted_spec = {
        **draft_spec,
        "trial_confirmation": [{"case_id": "R1", "acknowledged": True}],
    }
    rig_plugins.validate_run_submission(admitted_spec)
    result = control.submit_assignment({"kind": "run", "run_spec": admitted_spec}, "idem-ok")
    assert result["assignment_id"] == "asgn-1"
    assert len(store.submitted) == 1


# ── 3. autophone.executor Contract & RigAgent Integration ─────────────────────


def test_executor_entrypoint_precedence_cardinality_and_rig_agent_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template_eps: dict[str, dict[str, str]],
) -> None:
    target = _make_target(tmp_path)
    target_spec = next(iter(template_eps["autophone.executor"].values()))
    factory_fn = rig_main.load_executor_factory(target_spec)
    ep = _FakeEntryPoint("template_executor", target_spec, factory_fn)

    # Precedence: CLI flag > AUTOPHONE_AGENT_EXECUTOR_FACTORY > DEFAULT_EXECUTOR_FACTORY.
    monkeypatch.delenv("AUTOPHONE_AGENT_EXECUTOR_FACTORY", raising=False)
    default_args = rig_main.build_parser().parse_args(["--target", str(tmp_path / "target.json")])
    assert default_args.executor_factory == rig_main.DEFAULT_EXECUTOR_FACTORY

    monkeypatch.setenv("AUTOPHONE_AGENT_EXECUTOR_FACTORY", target_spec)
    env_args = rig_main.build_parser().parse_args(["--target", str(tmp_path / "target.json")])
    assert env_args.executor_factory == target_spec

    cli_args = rig_main.build_parser().parse_args([
        "--target", str(tmp_path / "target.json"),
        "--executor-factory", "custom.mod:factory",
    ])
    assert cli_args.executor_factory == "custom.mod:factory"

    # Cardinality checks in autophone.rig.plugins.create_executor.
    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.executor": []})
    with pytest.raises(RuntimeError, match="autophone.executor requires exactly one installed provider"):
        rig_plugins.create_executor(target=target, run_root=tmp_path)

    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.executor": [ep, ep]})
    with pytest.raises(RuntimeError, match="autophone.executor requires exactly one installed provider"):
        rig_plugins.create_executor(target=target, run_root=tmp_path)

    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.executor": [ep]})
    executor = rig_plugins.create_executor(target=target, run_root=tmp_path)

    def make_assignment(assignment_id: str, attempt_no: int = 1, fencing: int = 1) -> dict[str, Any]:
        return {
            "assignment_id": assignment_id,
            "kind": "run",
            "bound_host_id": target.host_id,
            "bound_rig_id": target.rig_id,
            "bound_generation": target.rig_generation,
            "attempt": {"attempt_no": attempt_no, "fencing": fencing},
            "run_spec": {"run_id": f"run-{assignment_id}", "device": target.rig_id},
        }

    # Normal execution via real RigAgent -> schema-valid assignment-result/v1.
    ok_transport = _FakeAgentTransport()
    ok_agent = RigAgent(
        target,
        ok_transport,  # type: ignore[arg-type]
        executor,
        source_identity={"head": "test", "dirty": False},
    )
    assert ok_agent.report_screen_quarantine(force=True) is True
    assert ok_transport.quarantine_reports[0]["quarantined"] is False

    completed = ok_agent.execute_assignment(make_assignment("asgn-pass", attempt_no=1, fencing=7))
    assert completed["status"] == "passed"
    assert len(ok_transport.completed) == 1
    receipt = ok_transport.completed[0][3]
    validate_or_raise("assignment-result.v1.json", receipt)
    assert receipt["product_verdict"] == "passed"
    assert receipt["run_result"] == {"run_id": "run-asgn-pass", "status": "passed"}

    # Cooperative cancellation -> returns clean cancelled receipt with Cleanup receipt.
    cancel_transport = _FakeAgentTransport(heartbeat_status="cancel_requested")
    cancel_agent = RigAgent(
        target,
        cancel_transport,  # type: ignore[arg-type]
        executor,
        source_identity={"head": "test", "dirty": False},
    )
    cancelled = cancel_agent.execute_assignment(make_assignment("asgn-cancel", attempt_no=2, fencing=8))
    assert cancelled["status"] == "cancelled"
    cancel_receipt = cancel_transport.completed[0][3]
    validate_or_raise("assignment-result.v1.json", cancel_receipt)
    assert cancel_receipt["product_verdict"] == "not_run"
    assert cancel_receipt["cleanup_receipt"]["device_access_released"] is True

    # Stale fencing -> raises StaleDeviceWrite and never calls complete().
    stale_transport = _FakeAgentTransport(write_valid=False)
    stale_agent = RigAgent(
        target,
        stale_transport,  # type: ignore[arg-type]
        executor,
        source_identity={"head": "test", "dirty": False},
    )
    with pytest.raises(StaleDeviceWrite):
        stale_agent.execute_assignment(make_assignment("asgn-stale", attempt_no=3, fencing=9))
    assert stale_transport.completed == []


# ── 4. autophone.device_session Contract & DeviceSessionAgent Integration ─────


def test_device_session_entrypoint_precedence_cardinality_and_agent_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template_eps: dict[str, dict[str, str]],
) -> None:
    target = _make_target(tmp_path)
    target_spec = next(iter(template_eps["autophone.device_session"].values()))
    factory_fn = rig_main.load_executor_factory(target_spec)
    ep = _FakeEntryPoint("template_device_session", target_spec, factory_fn)

    # Precedence: CLI flag > AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY > DEFAULT_DEVICE_SESSION_FACTORY.
    monkeypatch.delenv("AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY", raising=False)
    default_args = rig_main.build_parser().parse_args(["--target", str(tmp_path / "target.json")])
    assert default_args.device_session_factory == rig_main.DEFAULT_DEVICE_SESSION_FACTORY

    monkeypatch.setenv("AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY", target_spec)
    env_args = rig_main.build_parser().parse_args(["--target", str(tmp_path / "target.json")])
    assert env_args.device_session_factory == target_spec

    built_agent = rig_main.build_device_session_agent(env_args, target)
    assert built_agent.rig_id == target.rig_id
    assert built_agent.ops.__class__.__name__ == "TemplateDeviceSessionOps"

    # Cardinality checks in autophone.rig.plugins.create_device_session_ops.
    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.device_session": []})
    with pytest.raises(RuntimeError, match="autophone.device_session requires exactly one installed provider"):
        rig_plugins.create_device_session_ops(target=target, run_root=tmp_path)

    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.device_session": [ep, ep]})
    with pytest.raises(RuntimeError, match="autophone.device_session requires exactly one installed provider"):
        rig_plugins.create_device_session_ops(target=target, run_root=tmp_path)

    _patch_entry_points(monkeypatch, rig_plugins, {"autophone.device_session": [ep]})
    ops = rig_plugins.create_device_session_ops(target=target, run_root=tmp_path)
    assert "device_status" in DEVICE_SESSION_OPS

    # Drive real DeviceSessionAgent.run_forever() across valid ops, refusals, and disconnect cleanup.
    agent_holder: dict[str, DeviceSessionAgent] = {}
    scripted = [
        {"type": "ready"},
        {"type": "request", "request_id": "req-1", "device": target.rig_id, "op": "device_status", "payload": {}},
        {"type": "request", "request_id": "req-2", "device": target.rig_id, "op": "console_start", "payload": {"request_id": "idem-console"}},
        {"type": "request", "request_id": "req-3", "device": target.rig_id, "op": "nonexistent_op", "payload": {}},
        {"type": "request", "request_id": "req-4", "device": "other-rig", "op": "device_status", "payload": {}},
    ]
    fake_socket = _FakeSessionSocket(
        scripted,
        on_result_count=4,
        on_done=lambda: agent_holder["agent"]._stop.set(),
    )
    agent = DeviceSessionAgent(
        "ws://127.0.0.1:8100/api/control/device-session/ws",
        host_id=target.host_id,
        rig_id=target.rig_id,
        rig_generation=target.rig_generation,
        devices=[target.rig_id, target.vm_dir.name],
        ops=ops,
        connect=lambda _url, _timeout: fake_socket,
        reconnect_delay_s=0.01,
    )
    agent_holder["agent"] = agent
    agent.run_forever()

    results = {
        msg["request_id"]: msg
        for msg in fake_socket.sent
        if msg.get("type") == "result"
    }
    assert results["req-1"]["ok"] is True
    assert results["req-1"]["result"]["device"] == target.rig_id
    assert results["req-2"]["ok"] is True
    assert results["req-2"]["result"]["session_id"].startswith("tpl-")
    assert results["req-3"]["ok"] is False
    assert results["req-3"]["error"]["status"] == 422
    assert results["req-3"]["error"]["code"] == "unknown_op"
    assert results["req-4"]["ok"] is False
    assert results["req-4"]["error"]["status"] == 409
    assert results["req-4"]["error"]["code"] == "device_mismatch"

    # Exiting run_forever() must invoke ops.close_all("channel_disconnected") and clear active sessions.
    assert ops.call(target.rig_id, "screen_holder", {})["holder"] is None


# ── 5. Related Optional Entry Point Loaders ────────────────────────────────────


def test_optional_entrypoint_groups_follow_documented_fail_closed_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # autophone.run_handlers merges disjoint maps and rejects duplicates.
    _patch_entry_points(
        monkeypatch,
        rig_plugins,
        {
            "autophone.run_handlers": [
                _FakeEntryPoint("b", "b:h", lambda: {"custom_b": "B"}),
                _FakeEntryPoint("a", "a:h", lambda: {"custom_a": "A"}),
            ],
        },
    )
    assert rig_plugins.load_run_handlers() == {"custom_a": "A", "custom_b": "B"}

    _patch_entry_points(
        monkeypatch,
        rig_plugins,
        {
            "autophone.run_handlers": [
                _FakeEntryPoint("a", "a:h", lambda: {"dup": "A"}),
                _FakeEntryPoint("b", "b:h", lambda: {"dup": "B"}),
            ],
        },
    )
    with pytest.raises(ValueError, match="duplicate Run handler: dup"):
        rig_plugins.load_run_handlers()

    # autophone.console_extensions honors AUTOPHONE_DISABLE_EXTENSIONS.
    seen: list[str] = []
    _patch_entry_points(
        monkeypatch,
        console_extensions,
        {
            "autophone.console_extensions": [
                _FakeEntryPoint("cli", "ext:cli", lambda arg: seen.append(arg)),
            ],
        },
    )
    monkeypatch.setenv("AUTOPHONE_DISABLE_EXTENSIONS", "1")
    console_extensions.register_extensions("cli", "ignored")
    assert seen == []
    monkeypatch.delenv("AUTOPHONE_DISABLE_EXTENSIONS", raising=False)
    console_extensions.register_extensions("cli", "registered")
    assert seen == ["registered"]

    # autophone.candidate_lifecycle is lazy for ordinary runs and fails closed for candidate_ref.
    _patch_entry_points(monkeypatch, candidate_lifecycle, {"autophone.candidate_lifecycle": []})
    assert candidate_lifecycle.activate_run_candidate({"type": "regression"}, None, Path(".")) is None
    with pytest.raises(RuntimeAdmissionError, match="candidate_lifecycle_provider_unavailable"):
        candidate_lifecycle.activate_run_candidate(
            {"type": "regression", "candidate_ref": {"id": "cand-1"}},
            None,
            Path("."),
        )
