# 公开 Entry Point 扩展指南

**简体中文** | [English](extensions.en.md)

Autophone 公开核心将控制面/Rig 传输层（`autophone-rig`）与回归执行、试跑准入及交互会话实现（`autophone-domain`）通过标准 Python `importlib.metadata.entry_points` 解耦。第三方或自定义部署可以在不修改核心代码的前提下，通过分发包元数据或显式工厂参数替换或接入自己的实现。

> 本文档所有函数签名、加载优先级与 fail-closed 契约均与当前源码一致，并由 [tests/test_extension_entrypoints.py](https://github.com/zhaoawd/autophone/blob/main/tests/test_extension_entrypoints.py) 持续校验。最小可运行插件模板见 [examples/plugin-template/](https://github.com/zhaoawd/autophone/blob/main/examples/plugin-template/README.md)。

## 1. 核心扩展点总览

| Entry Point 组 | 默认内置提供方（`autophone-domain`） | 加载器 | 基数约束 | 显式覆写方式 | 缺失/冲突行为（Fail-Closed） |
| --- | --- | --- | --- | --- | --- |
| `autophone.run_admission` | `trial_confirmation = "autophone.knowledge.trial_confirmation:validate_trial_confirmation"` | `autophone.rig.plugins.validate_run_submission(spec)` | 严格 `1` 个 | 无 CLI/环境变量覆写 | 抛出 `ContractError`（`ControlPlane` 转为 `ControlStateError`，HTTP `422`） |
| `autophone.executor` | `run = "autophone.execution.handlers:create_executor"` | `autophone.rig.plugins.create_executor(**kwargs)` | 严格 `1` 个 | `--executor-factory` / `AUTOPHONE_AGENT_EXECUTOR_FACTORY` | 抛出 `RuntimeError` |
| `autophone.device_session` | `run = "autophone.execution.handlers:create_device_session_ops"` | `autophone.rig.plugins.create_device_session_ops(**kwargs)` | 严格 `1` 个 | `--device-session-factory` / `AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY`（或 `--no-device-session` 关闭） | 抛出 `RuntimeError` |

> [!IMPORTANT]
> **与 `autophone-domain` 共存规则**：上述三个扩展点在 `autophone.rig.plugins` 中都强制校验 **`len(entries) == 1`**。如果在已安装 `autophone-domain` 的同一个 Python 环境中再安装另一个声明了相同 `[project.entry-points."..."]` 组的分发包，默认发现路径会因发现 `2` 个提供方而立即 fail-closed 报错。
> - **与 `autophone-domain` 共存时**：不要在第三方包的 `pyproject.toml` 中重复声明上述 3 个 entry point 组，改为直接通过 `--executor-factory` / `AUTOPHONE_AGENT_EXECUTOR_FACTORY` 与 `--device-session-factory` / `AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY` 显式指向自定义工厂函数。
> - **精简 `autophone-rig` 独立部署（未安装 `autophone-domain`）时**：保留 `[project.entry-points."..."]` 声明，使 `autophone.rig.plugins` 自动发现唯一的第三方提供方。

---

## 2. `autophone.run_admission`

用于在不让 `autophone-rig` 反向依赖 `autophone-domain` 的前提下，由控制面在入队前校验高风险草稿试跑确认（Draft Trial Admission）。

### 调用链与触发条件

- **调用位置**：`autophone.control.service.ControlPlane.submit_assignment(plan, idempotency_key)` 在 `isinstance(plan.get("run_spec"), Mapping)` 时调用 `autophone.rig.plugins.validate_run_submission(plan["run_spec"])`。
- **前置类型检查**：先校验 `case_selector`（缺省视为空字典）；若非 `dict`，直接抛出 `ContractError("trial_confirmation_selector_invalid: case_selector must be an object")`。
- **惰性触发条件**：仅当满足以下任一条件时才会解析并调用 `autophone.run_admission`：
  1. `spec.get("trial_confirmation") is not None`；或
  2. `spec.get("type") == "regression"` 且（`"draft" in (selector.get("statuses") or [])` 或 `bool(selector.get("trial_draft_recipes"))`）。
- 普通 `active` 回归（未携带 `trial_confirmation` 且未选择草稿）会直接返回 `None`，不会查找该 entry point。

### 接口签名与错误契约

```python
from collections.abc import Mapping
from typing import Any
from autophone.contracts import ContractError

def validate_run_admission(spec: Mapping[str, Any]) -> None:
    ...
```

- **入参**：单个位置参数 `spec`（即 `run_spec` 映射对象）。内置实现 `validate_trial_confirmation(spec, profile=None)` 兼容单参调用。
- **返回值**：通过准入时返回 `None`（返回值被忽略）。
- **拒绝契约**：抛出 `autophone.contracts.ContractError("<reason_code>: <detail>")`。`ControlPlane.submit_assignment` 会将其捕获并转换为 `ControlStateError` 拒绝入队（看板 `POST /runs` 返回 HTTP `422`）。
- **基数门禁**：若环境中已安装的 `autophone.run_admission` 提供方数量不等于 `1`（`0` 或 `>= 2`），抛出 `ContractError("trial_confirmation_validator_unavailable: install one run admission provider")`。

> [!NOTE]
> 在完整安装 `autophone-console` + `autophone-domain` 的进程中，领域侧提交入口（`autophone/adapters/http/runs.py`、`autophone/execution/dispatch.py`、`autophone/execution/run_service.py` 与 `autophone/execution/run_execution.py`）也会直接调用内置 `validate_trial_confirmation`。`autophone.run_admission` 主要服务仅依赖 `autophone-rig` 的独立控制面进程。

---

## 3. `autophone.executor`

为 `autophone-rig-agent` 构造 Assignment 执行器，负责在 `DeviceWriteFence` 与 `AssignmentControl` 保护下执行派单并返回标准化的 `assignment-result/v1` 回执。

### 加载优先级（由高到低）

由 `autophone/rig/main.py` 解析：

1. **CLI 参数**：`autophone-rig-agent --executor-factory <module:function>`
2. **环境变量**：`AUTOPHONE_AGENT_EXECUTOR_FACTORY=<module:function>`
3. **默认工厂**：`autophone.rig.plugins:create_executor` → 通过 `importlib.metadata.entry_points(group="autophone.executor")` 解析唯一提供方

`load_executor_factory(spec)` 严格校验 `module:function` 语法（格式错误抛 `ValueError`）以及解析结果可调用（不可调用抛 `TypeError`）。默认工厂发现 `0` 个或 `>= 2` 个提供方时抛出：

```text
RuntimeError: autophone.executor requires exactly one installed provider; install autophone-domain or supply an explicit factory
```

### 工厂签名与运行前环境

`autophone/rig/main.py` 在调用工厂前会先执行 `os.environ.update(target.process_environment())`，随后以关键字参数调用工厂：

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

### 返回对象协议（`AgentAssignmentExecutor`）

- **必需方法**：
  ```python
  def execute(
      self,
      assignment: Mapping[str, Any],
      context: AgentExecutionContext,
  ) -> Mapping[str, Any]: ...
  ```
- **可选方法**：`screen_quarantine_report(self, target: DeviceTarget) -> Mapping[str, Any]`，供 `RigAgent.report_screen_quarantine()` 向控制面上报本机 `ScreenLock` 隔离标记（格式 `screen-quarantine-report/v1`）。
- **`AgentExecutionContext` 字段**（`autophone/rig/agent.py`）：
  - `target: DeviceTarget` — 当前 Rig 身份与路径（`host_id`、`rig_id`、`rig_generation`、`socket`、`vm_dir`、`runtime_dir`、`log_dir`、`temp_dir`、`package_cache`、`protocol` 等）。
  - `write_fence: DeviceWriteFence` — 每次写设备前调用 `context.write_fence.check()`（或 `context.write_fence.run(fn, *args, **kwargs)`）；收尾释放资源时调用 `context.write_fence.check_release()`。
  - `journal: AgentJournal` — 本机执行日志（`agent-execution.jsonl`）。
  - `assignment_control: AssignmentControl` — 阶段边界调用 `context.check_control()` 检查租约与取消；进入 Cleanup 时调用 `context.assignment_control.begin_cleanup()`，此后清理动作不再因取消被拒，但仍受租约与 fencing 约束。

### 返回值与 Fail-Closed 语义

- **返回结构**：必须满足 `autophone/_data/schemas/assignment-result.v1.json`（`additionalProperties: false`，完整 Schema 见 [核心 JSON Schema 字典](../reference/schemas.md)），包含必需字段：`schema_version`（`"assignment-result/v1"`）、`assignment_id`、`attempt_no`、`fencing`、`status`（`passed | failed | error | blocked | cancelled | indeterminate`）、`product_verdict`（有 `run_result` 时必须等于 `run_result["status"]`，无 `run_result` 时为 `"not_run"`）、`run_result`、`cleanup_receipt`（`status`、`artifacts_sealed`、`device_access_released`）、`rig_disposition`（`reusable | needs_inspection | quarantine`）、`fixture_disposition`（`preserved | unknown | restored | failed | not_required`）、`artifact_refs`、`closure`。
- **异常与保护行为**（`RigAgent._execute_assignment()`）：
  - `AssignmentCancelled` 未经清理直接逃逸 → 记日志 `executor_cancelled_without_cleanup` 并抛 `ExecutorContractError`。
  - `StaleDeviceWrite` → 记日志 `stale_fencing` 并原样重抛，**不提交完成回执**。
  - 其余未捕获异常 → 记日志 `executor_failed_without_cleanup` 并抛 `ExecutorContractError`（绝不伪造 Cleanup 回执）。
  - 返回 `reason == "device_quiesce_unverified"` → 终态提交成功后 `RigAgent` 抛 `AgentRestartRequired` 退出进程，防止残留句柄污染后续派单。

---

## 4. `autophone.device_session`

为 `autophone-rig-agent` 构造交互通道执行面（`DeviceSessionAgent`），承载远程设备控制台、屏幕预览、系统定位与网页录制会话。

### 加载优先级（由高到低）

由 `autophone/rig/main.py` 解析：

1. **禁用开关**：传入 `autophone-rig-agent --no-device-session` 时跳过交互通道构造与连接。
2. **CLI 参数**：`autophone-rig-agent --device-session-factory <module:function>`
3. **环境变量**：`AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY=<module:function>`
4. **默认工厂**：`autophone.rig.plugins:create_device_session_ops` → 通过 `importlib.metadata.entry_points(group="autophone.device_session")` 解析唯一提供方

默认工厂发现 `0` 个或 `>= 2` 个提供方时抛出：

```text
RuntimeError: autophone.device_session requires exactly one installed provider; install autophone-domain or supply an explicit factory
```

### 工厂签名与返回协议（`DeviceSessionExecutor`）

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

返回对象必须实现 `autophone.rig.device_session.DeviceSessionExecutor` 协议：

- `call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]`
- `close_all(self, reason: str) -> dict[str, Any]`

### 操作白名单、拒绝码与掉线释放

- **标准操作白名单**：定义于 `autophone.application.device_session.DEVICE_SESSION_OPS` 与 `autophone.rig.session_protocol.RECORDING_SESSION_OPS`：
  - 状态 / 锁 / 定位 / 预览：`device_status`、`screen_holder`、`system_location`、`location_lock_status`、`preview_frame`
  - 远程控制台：`console_device_status`、`console_start`、`console_view`、`console_frame`、`console_event`、`console_heartbeat`、`console_stop`
  - 录制会话：`record_start`、`record_view`、`record_frame`、`record_heartbeat`、`record_event`、`record_label`、`record_finish`、`record_cancel`、`record_claim`、`record_desktop_cancel`
- **结构化拒绝**：未知设备、未知操作、锁冲突或业务拒绝应抛出 `autophone.application.device_session.DeviceSessionRefused(message, status=..., code=...)`。`DeviceSessionAgent._answer()` 会将 `exc.to_dict()` 原样封装为 `{"type": "result", "ok": False, "error": {...}}` 返回控制面。
- **兜底 fail-closed**：`call()` 抛出的其他未捕获异常会被 `DeviceSessionAgent` 收敛为 `{"status": 503, "code": "device_error", "message": "<ExcType>: <msg>"}`，防止提交侧挂起。
- **掉线立即还锁**：`DeviceSessionAgent.run_forever()` 读循环因任何原因退出时，其 `finally` 块必定调用 `ops.close_all("channel_disconnected")`。实现必须释放所有活跃会话与 `ScreenLock` lease，并返回包含 `closed_sessions`（列表）、`closed_previews`、`reason` 的字典。

---

## 5. 其他可选 Entry Point 组（完整清单）

除上述 3 个核心扩展点外，源码中还存在以下可选扩展点，供高级定制参考：

| Entry Point 组 | 加载器 | 基数与排序 | 禁用 / 触发条件 | 冲突或缺失行为 |
| --- | --- | --- | --- | --- |
| `autophone.run_handlers` | `autophone.rig.plugins.load_run_handlers()` | `0..N`（按 `entry.name` 排序），每个入口返回 `dict[str, RunHandler]` | 在 `build_run_handlers()` 装配内置 `regression`、`verification`、`operation` 后合并 | 与内置或其他插件同名时抛 `ValueError("duplicate Run handler: ...")` |
| `autophone.console_extensions` | `autophone.adapters.extensions.register_extensions(name, *args)` | 按名称匹配 `0..N`：`cli`、`package`、`http` | 设置 `AUTOPHONE_DISABLE_EXTENSIONS=1\|true\|yes` 时完全跳过 | 异常直接向上传播 |
| `autophone.candidate_lifecycle` | `autophone.execution.candidate_lifecycle._provider()` | 严格 `1` 个（实现 `CandidateLifecycle` 协议） | **仅当** `run_spec` 含非空 `candidate_ref` 时触发；普通 Run 不加载 | 缺失或重名抛 `RuntimeAdmissionError(["candidate_lifecycle_provider_unavailable"])` |

---

## 6. `pyproject.toml` 注册与最小模板

完整的最小第三方插件模板位于 [examples/plugin-template/](https://github.com/zhaoawd/autophone/blob/main/examples/plugin-template/README.md)（参见 [examples/plugin-template/pyproject.toml](https://github.com/zhaoawd/autophone/blob/main/examples/plugin-template/pyproject.toml)）：

```toml
[project.entry-points."autophone.run_admission"]
template_admission = "autophone_plugin_template.admission:validate_run_admission"

[project.entry-points."autophone.executor"]
template_executor = "autophone_plugin_template.executor:create_executor"

[project.entry-points."autophone.device_session"]
template_device_session = "autophone_plugin_template.device_session:create_device_session_ops"
```

### 验证步骤

1. **运行仓库自带的协议防漂移测试**（校验内置 `autophone-domain` 元数据、模板 `pyproject.toml`、三大加载器优先级、fail-closed 基数门禁以及 `ControlPlane` / `RigAgent` / `DeviceSessionAgent` 端到端交互）：
   ```bash
   uv run --group platform --group contract pytest tests/test_extension_entrypoints.py
   ```
2. **检查当前环境已注册的提供方数量**（确保严格为 `1` 个）：
   ```bash
   python3 -c '
   from importlib.metadata import entry_points
   for group in ("autophone.run_admission", "autophone.executor", "autophone.device_session"):
       eps = list(entry_points(group=group))
       print(group, [(ep.name, ep.value) for ep in eps])
   '
   ```
3. **与 `autophone-domain` 共存时通过显式工厂启动 `autophone-rig-agent`**：
   ```bash
   autophone-rig-agent \
     --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
     --executor-factory autophone_plugin_template.executor:create_executor \
     --device-session-factory autophone_plugin_template.device_session:create_device_session_ops
   ```

另见[多 VM 控制面部署](multi-vm-control-plane.md)、[回归运行期契约](../concepts/runtime-contract.md)与[核心分发安装与验证](../public-core.md)。
