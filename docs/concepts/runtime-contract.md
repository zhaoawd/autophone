# 回归运行期契约

> 适用范围：Autophone 确定性回归执行器（`RunService._execute_phase2()`）。定位能力细节见[定位能力域](../domains/location.md)。

## 1. 运行顺序

在持有所选设备的 `ScreenLock` 期间，一次回归 Run 按以下顺序执行：

1. **Asset Admission（资产准入）**：默认回归只选择 `active` + `ui_ready` 且具备有效 live Recipe 的用例；不满足准入的资产不触达设备，直接记录 `blocked`（`scope=asset`）。显式传入 `--draft-cases` 并配合 `--ids`（及试跑副作用确认卡）时可试跑草稿用例。
2. **Requirement Derivation（能力推导）**：从默认策略、App Profile、Recipe 和 Operation 推导每条用例的能力要求（`vm_uds`、`app_ui`、`api_capture`、`location_injection` 等）。
3. **Rig Readiness（设备准备）**：探测 VM UDS 与健康状态；有界解锁锁屏（优先使用 guest 原生锁屏状态探针，不可用时回退截图 OCR，不输入密码）；检查并冷重启带 Frida Tag 255 残留的被测 App；核对 `app_list` PID 与就绪标记。
4. **Capability Planning（能力规划）**：仅阻断缺少 `required` 能力的用例；`advisory` 能力缺失不阻断 UI 用例执行。
5. **Deterministic Execution & Evidence（确定性执行与报告）**：按 Typed-IR 逐步派发 Verb/Operation，落盘 `platform-run-result.json`、`artifact-manifest.json`、`run.log` 与 `report.html`。

正式回归运行期**严禁调用 `autophone.application.ai` 临场决策**。

## 2. Blocker 分类

每条 `blocked` 用例均携带结构化 `blocker`（含 `scope`、`code`、可选 `capability`、`recovery` 与 `evidence_ref`）：

| 失因 | `scope` | 典型 `code` |
| --- | --- | --- |
| 资产未满足准入 | `asset` | `missing_live_recipe` |
| VM / App 基础环境不可用 | `rig` | `vm_uds_unavailable`、`app_ui_unavailable`、`springboard_restarted`、`run_aborted` |
| 可选平台能力缺失 | `capability` | `location_keeper_unavailable`、`api_capture_unavailable` |
| 业务前置或会话不成立 | `fixture` | `missing_location_anchor`、`authenticated_session_unavailable` |

## 3. 总体状态与 CLI 退出码

| Run `status` | 含义 | CLI 退出码 |
| --- | --- | ---: |
| `passed` | 所有计划用例通过（`advisory` 面可为 `pending`） | `0` |
| `failed` | 存在断言不符（`product`）或资产债务（`asset`） | `1` |
| `partial` | 无产品失败，但存在能力 / 前置 / 组级阻断 | `3` |
| `blocked` | 基础 Rig 不可用或执行前中止，未进入 UI 执行 | `4` |
| `error` | 编排、协议、超时或空断言等未预期错误 | `5` |
| `pending` | 存在尚未回填裁决的 `required` 观测面 | `6` |

> 配置了 Control Plane 但控制面不可达、或参数/锁/权限校验拒绝时，CLI 输出结构化拒绝 JSON 并以退出码 `2`（锁冲突为 `3`）退出，**绝不静默绕过控制面直连设备**。

## 4. 超时与防假绿边界

- 单条用例默认具备有界超时（300s），超时归类为 `error` 而非产品 `failed`。
- 正常执行完所有步骤但未执行任何 `assert_*` 动作的 Recipe 强制判定为 `error`，防止空脚本误报通过。
- 控制面模式下，Run 由目标 Rig Agent 独占执行；若进程中断导致状态不明，控制面按租约回收并记录恢复证据。
