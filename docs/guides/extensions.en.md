# Public Entry Point Extension Guide

[简体中文](extensions.md) | **English**

Autophone Public Core decouples the Control Plane and Rig transport layer (`autophone-rig`) from regression execution, draft-trial admission, and interactive session implementations (`autophone-domain`) using standard Python `importlib.metadata.entry_points`. Third-party integrations and custom deployments can plug in their own providers without modifying core packages.

> Every signature, loading precedence rule, and fail-closed contract in this guide is verified directly against the real loaders in [tests/test_extension_entrypoints.py](../../tests/test_extension_entrypoints.py). A minimal runnable template package lives in [examples/plugin-template/](../../examples/plugin-template/README.md).

## 1. Core Entry Point Overview

| Entry Point Group | Built-In Provider (`autophone-domain`) | Loader | Cardinality | Explicit Override | Missing / Conflict Behavior (Fail-Closed) |
| --- | --- | --- | --- | --- | --- |
| `autophone.run_admission` | `trial_confirmation = "autophone.knowledge.trial_confirmation:validate_trial_confirmation"` | `autophone.rig.plugins.validate_run_submission(spec)` | Exactly `1` | None (entry-point lookup only) | Raises `ContractError` (`ControlPlane` translates to `ControlStateError`, HTTP `422`) |
| `autophone.executor` | `run = "autophone.execution.handlers:create_executor"` | `autophone.rig.plugins.create_executor(**kwargs)` | Exactly `1` | `--executor-factory` / `AUTOPHONE_AGENT_EXECUTOR_FACTORY` | Raises `RuntimeError` |
| `autophone.device_session` | `run = "autophone.execution.handlers:create_device_session_ops"` | `autophone.rig.plugins.create_device_session_ops(**kwargs)` | Exactly `1` | `--device-session-factory` / `AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY` (or `--no-device-session`) | Raises `RuntimeError` |

> [!IMPORTANT]
> **Coexistence with `autophone-domain`**: All three groups above enforce **exact cardinality `len(entries) == 1`** inside `autophone.rig.plugins`. Installing a second package that registers the same `[project.entry-points."..."]` group alongside `autophone-domain` in the same Python environment causes default resolution to fail closed (`2` providers found).
> - **When coexisting with `autophone-domain`**: Omit those overlapping `[project.entry-points."..."]` tables in your package's `pyproject.toml`, and pass `--executor-factory` / `AUTOPHONE_AGENT_EXECUTOR_FACTORY` and `--device-session-factory` / `AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY` explicitly on `autophone-rig-agent`.
> - **In a minimal `autophone-rig`-only deployment (without `autophone-domain`)**: Keep the `[project.entry-points."..."]` tables declared so `autophone.rig.plugins` auto-discovers your sole provider.

---

## 2. `autophone.run_admission`

Allows the Control Plane (`autophone-rig`) to enforce draft-trial submission admission without importing `autophone-domain`.

### Call Path & Trigger Conditions

- **Caller**: `autophone.control.service.ControlPlane.submit_assignment(plan, idempotency_key)` calls `autophone.rig.plugins.validate_run_submission(plan["run_spec"])` whenever `isinstance(plan.get("run_spec"), Mapping)`.
- **Pre-check**: `selector = spec.get("case_selector") or {}` must be a `dict`; otherwise `validate_run_submission` immediately raises `ContractError("trial_confirmation_selector_invalid: case_selector must be an object")`.
- **Lazy trigger**: `autophone.run_admission` is only resolved and invoked when either:
  1. `spec.get("trial_confirmation") is not None`; or
  2. `spec.get("type") == "regression"` and (`"draft" in (selector.get("statuses") or [])` or `bool(selector.get("trial_draft_recipes"))`).
- Ordinary `active` runs without `trial_confirmation` or draft selectors return `None` immediately without looking up this entry point.

### Callable Signature & Error Contract

```python
from collections.abc import Mapping
from typing import Any
from autophone.contracts import ContractError

def validate_run_admission(spec: Mapping[str, Any]) -> None:
    ...
```

- **Arguments**: A single positional mapping argument `spec` (the `run_spec` dict). The built-in `validate_trial_confirmation(spec, profile=None)` accepts this single positional argument.
- **Return value**: `None` on success (any return value is ignored).
- **Rejection**: Raise `autophone.contracts.ContractError("<reason_code>: <detail>")`. `ControlPlane.submit_assignment` catches `ContractError` and raises `ControlStateError` before touching the backing store (surfaced as HTTP `422` by `POST /runs`).
- **Cardinality gate**: If `0` or `>= 2` providers are installed in `autophone.run_admission`, raises `ContractError("trial_confirmation_validator_unavailable: install one run admission provider")`.

> [!NOTE]
> In full `autophone-console` + `autophone-domain` processes, local domain submission paths (`autophone/adapters/http/runs.py`, `autophone/execution/dispatch.py`, `autophone/execution/run_service.py`, `autophone/execution/run_execution.py`) also invoke `validate_trial_confirmation` directly. `autophone.run_admission` specifically decouples `autophone.control.service.ControlPlane` (`autophone-rig`) from `autophone-domain`.

---

## 3. `autophone.executor`

Constructs the Assignment executor inside `autophone-rig-agent` to run leased device work under `DeviceWriteFence` and `AssignmentControl` and return a schema-valid `assignment-result/v1` receipt.

### Loading Precedence (Highest to Lowest)

Resolved by `autophone/rig/main.py`:

1. **CLI flag**: `autophone-rig-agent --executor-factory <module:function>`
2. **Environment variable**: `AUTOPHONE_AGENT_EXECUTOR_FACTORY=<module:function>`
3. **Default factory**: `autophone.rig.plugins:create_executor` → resolves the sole installed `importlib.metadata.entry_points(group="autophone.executor")` provider

`load_executor_factory(spec)` enforces `module:function` syntax (raising `ValueError` otherwise) and verifies the resolved attribute is callable (raising `TypeError` otherwise). When `0` or `>= 2` entry points are installed, the default factory raises:

```text
RuntimeError: autophone.executor requires exactly one installed provider; install autophone-domain or supply an explicit factory
```

### Factory Signature & Pre-Bind Environment

Before calling the factory, `autophone/rig/main.py` applies `os.environ.update(target.process_environment())`, then calls the factory with keyword arguments:

```python
from pathlib import Path
from autophone.rig.agent import AgentAssignmentExecutor
from autophone.rig.target import DeviceTarget

def create_executor(
    *,
    target: DeviceTarget,
    run_root: Path | None = None,
) -> AgentAssignmentExecutor:
    ...
```

### Returned Object Protocol (`AgentAssignmentExecutor`)

- **Required method**:
  ```python
  def execute(
      self,
      assignment: Mapping[str, Any],
      context: AgentExecutionContext,
  ) -> Mapping[str, Any]: ...
  ```
- **Optional method**: `screen_quarantine_report(self, target: DeviceTarget) -> Mapping[str, Any]`, queried by `RigAgent.report_screen_quarantine()` to report local `ScreenLock` quarantine markers (`screen-quarantine-report/v1`).
- **`AgentExecutionContext` attributes** (`autophone/rig/agent.py`):
  - `target: DeviceTarget` — immutable Rig identity and local paths (`host_id`, `rig_id`, `rig_generation`, `socket`, `vm_dir`, `runtime_dir`, `log_dir`, `temp_dir`, `package_cache`, `protocol`, etc.).
  - `write_fence: DeviceWriteFence` — call `context.write_fence.check()` (or `context.write_fence.run(fn, *args, **kwargs)`) before every device-mutating action; call `context.write_fence.check_release()` when releasing held resources during Cleanup.
  - `journal: AgentJournal` — local execution journal (`agent-execution.jsonl`).
  - `assignment_control: AssignmentControl` — call `context.check_control()` at stage boundaries; call `context.assignment_control.begin_cleanup()` upon entering Cleanup so cleanup operations are no longer blocked by cancellation (while remaining fenced by Lease/Fencing).

### Return Value & Fail-Closed Semantics

- **Return schema**: Must conform to `autophone/_data/schemas/assignment-result.v1.json` (`additionalProperties: false`; see [JSON Schema Reference](../reference/schemas.md)), providing all required fields: `schema_version` (`"assignment-result/v1"`), `assignment_id`, `attempt_no`, `fencing`, `status` (`passed | failed | error | blocked | cancelled | indeterminate`), `product_verdict` (must equal `run_result["status"]` when `run_result` is present, or `"not_run"` when `run_result` is `None`), `run_result`, `cleanup_receipt` (`status`, `artifacts_sealed`, `device_access_released`), `rig_disposition` (`reusable | needs_inspection | quarantine`), `fixture_disposition` (`preserved | unknown | restored | failed | not_required`), `artifact_refs`, and `closure`.
- **Exception & fail-closed handling** (`RigAgent._execute_assignment()`):
  - Escaping `AssignmentCancelled` without a Cleanup receipt → logged as `executor_cancelled_without_cleanup`, raises `ExecutorContractError`.
  - `StaleDeviceWrite` → logged as `stale_fencing` and re-raised untouched without calling `complete()`.
  - Any other uncaught `Exception` → logged as `executor_failed_without_cleanup`, raises `ExecutorContractError` (never fabricates a Cleanup receipt).
  - Returning `reason == "device_quiesce_unverified"` → once Control Plane records completion, `RigAgent` raises `AgentRestartRequired` and exits rather than accepting further work with potentially leaked handles.

---

## 4. `autophone.device_session`

Constructs the interactive session executor (`DeviceSessionAgent`) inside `autophone-rig-agent` for the Web Device Console, screen previews, system location, and interactive recording.

### Loading Precedence (Highest to Lowest)

Resolved by `autophone/rig/main.py`:

1. **Disable flag**: Passing `autophone-rig-agent --no-device-session` skips constructing and starting the interactive channel altogether.
2. **CLI flag**: `autophone-rig-agent --device-session-factory <module:function>`
3. **Environment variable**: `AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY=<module:function>`
4. **Default factory**: `autophone.rig.plugins:create_device_session_ops` → resolves the sole installed `importlib.metadata.entry_points(group="autophone.device_session")` provider

When `0` or `>= 2` entry points are installed, the default factory raises:

```text
RuntimeError: autophone.device_session requires exactly one installed provider; install autophone-domain or supply an explicit factory
```

### Factory Signature & Protocol (`DeviceSessionExecutor`)

```python
from pathlib import Path
from typing import Any
from autophone.rig.device_session import DeviceSessionExecutor
from autophone.rig.target import DeviceTarget

def create_device_session_ops(
    *,
    target: DeviceTarget,
    run_root: Path | None = None,
) -> DeviceSessionExecutor:
    ...
```

The returned object must implement `autophone.rig.device_session.DeviceSessionExecutor`:

- `call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]`
- `close_all(self, reason: str) -> dict[str, Any]`

### Operation Allowlists, Refusals & Disconnect Cleanup

- **Standard operations**: Defined by `autophone.application.device_session.DEVICE_SESSION_OPS` and `autophone.rig.session_protocol.RECORDING_SESSION_OPS`:
  - Status / lock / location / preview: `device_status`, `screen_holder`, `system_location`, `location_lock_status`, `preview_frame`
  - Remote console: `console_device_status`, `console_start`, `console_view`, `console_frame`, `console_event`, `console_heartbeat`, `console_stop`
  - Interactive recording: `record_start`, `record_view`, `record_frame`, `record_heartbeat`, `record_event`, `record_label`, `record_finish`, `record_cancel`, `record_claim`, `record_desktop_cancel`
- **Structured refusals**: Raise `autophone.application.device_session.DeviceSessionRefused(message, status=..., code=...)` for unknown devices, unknown operations, lock conflicts, or business refusals. `DeviceSessionAgent._answer()` serializes `exc.to_dict()` verbatim in the WebSocket result frame (`"ok": False`).
- **Fail-closed fallback**: Any other uncaught exception from `call()` is converted to `{"status": 503, "code": "device_error", "message": "<ExcType>: <msg>"}` so the caller never stalls.
- **Disconnect lease release**: Whenever `DeviceSessionAgent.run_forever()` exits a connection loop, its `finally` block unconditionally invokes `ops.close_all("channel_disconnected")`. Providers must release all active console/recording sessions and `ScreenLock` leases immediately and return a dict containing `closed_sessions` (list), `closed_previews`, and `reason`.

---

## 5. Additional Optional Entry Point Groups

For completeness, Autophone also defines three additional optional entry-point groups in source:

| Entry Point Group | Loader | Cardinality & Ordering | Gate / Disable Switch | Conflict or Missing Behavior |
| --- | --- | --- | --- | --- |
| `autophone.run_handlers` | `autophone.rig.plugins.load_run_handlers()` | `0..N` (sorted by `entry.name`), each returning `dict[str, RunHandler]` | Merged after built-in `regression`, `verification`, `operation` handlers in `build_run_handlers()` | Raises `ValueError("duplicate Run handler: ...")` on key collision |
| `autophone.console_extensions` | `autophone.adapters.extensions.register_extensions(name, *args)` | `0..N` per hook name (`cli`, `package`, `http`) | Skipped entirely when `AUTOPHONE_DISABLE_EXTENSIONS` is `1`, `true`, or `yes` | Exceptions propagate directly |
| `autophone.candidate_lifecycle` | `autophone.execution.candidate_lifecycle._provider()` | Exactly `1` (`CandidateLifecycle` protocol) | Lazy: only loaded when `run_spec` has a non-`None` `candidate_ref`; ordinary runs never load it | Raises `RuntimeAdmissionError(["candidate_lifecycle_provider_unavailable"])` |

---

## 6. `pyproject.toml` Registration, Minimal Template & Verification

A complete minimal third-party package template is provided under [examples/plugin-template/](../../examples/plugin-template/README.md) (see [examples/plugin-template/pyproject.toml](../../examples/plugin-template/pyproject.toml)):

```toml
[project.entry-points."autophone.run_admission"]
template_admission = "autophone_plugin_template.admission:validate_run_admission"

[project.entry-points."autophone.executor"]
template_executor = "autophone_plugin_template.executor:create_executor"

[project.entry-points."autophone.device_session"]
template_device_session = "autophone_plugin_template.device_session:create_device_session_ops"
```

### Verification Steps

1. **Run the repository protocol-verification suite** (validates `autophone-domain` metadata, the template's `pyproject.toml`, loader precedence, fail-closed cardinality gates, and real `ControlPlane` / `RigAgent` / `DeviceSessionAgent` round-trips):
   ```bash
   uv run --group platform --group contract pytest tests/test_extension_entrypoints.py
   ```
2. **Inspect active entry points in your Python environment** (confirm exact cardinality = `1` for any group resolved by default):
   ```bash
   python3 -c '
   from importlib.metadata import entry_points
   for group in ("autophone.run_admission", "autophone.executor", "autophone.device_session"):
       eps = list(entry_points(group=group))
       print(group, [(ep.name, ep.value) for ep in eps])
   '
   ```
3. **Launch `autophone-rig-agent` with explicit factories alongside `autophone-domain`**:
   ```bash
   autophone-rig-agent \
     --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
     --executor-factory autophone_plugin_template.executor:create_executor \
     --device-session-factory autophone_plugin_template.device_session:create_device_session_ops
   ```

See also: [Multi-VM Control Plane Deployment](multi-vm-control-plane.md), [Regression Runtime Contract](../concepts/runtime-contract.md), and [Core Distribution & Standalone Install](../public-core.md).
