# 核心 JSON Schema 字典（自动生成）

> 本文档由 `scripts/generate_public_docs.py` 从 `autophone/_data/schemas/` 核心契约文件自动生成，请勿手工修改。

## App Profile (`profile.yaml`)

- Schema 文件：`autophone/_data/schemas/app-profile.v1.json`
- 说明：接入一个被测 App 的最小契约。capabilities 声明式，缺什么降级什么。
- 必填字段：`app`、`bundle_id`、`display_name`、`schema_version`

| 字段 | 类型 / 枚举 | 必填 | 说明 |
| --- | --- | --- | --- |
| `allowed_effects` | `array<string>` | 否 | A3 effect policy：该 app 显式允许触发的不可逆/对外 effect verb；缺省空=全拒。迁移第一阶段仅 audit/report-only，enforce 后由 validate_recipe_for_app/运行时兜底使用。 |
| `app` | `string` | 是 | 目录名 / 标识符 |
| `artifact_store` | anyOf | 否 | 产品项目持有的不可变 IPA 字节存储；Package Store 仍持有 Build Record、Candidate 和安装回执。 |
| `build_label` | `string` | 否 | 当前在测 build 标识（ADR-0001 区分力闸 build-stamped 失效基准）：区分力证据的 build_label 与此不符即判 stale。缺省=不做 staleness 比对 |
| `bundle_id` | `string` | 是 | — |
| `capabilities` | `object` | 否 | 声明哪些观测面/能力可用；缺失即该面降级为 not_configured |
| `cases_dir` | `string` | 否 | — |
| `display_name` | `string` | 是 | — |
| `env` | `string` | 否 | — |
| `im` | `object` | 否 | B1 IM 域硬前提（红线『错误接收人率=0』）：send_* 类动作执行前必须断出当前会话稳定 ID 且 ∈ allowed_conversations。这是 allowed_effects（管『能不能发』）之上的第二道闸（管『能发给谁』）。缺省=零白名单=一切发送 abort。 |
| `lifecycle` | enum: `'active'` \| `'archived'` | 否 | App Profile 自身生命周期；缺省 active。archived 仅退出默认工作区，不物理删除档案、资产或历史 Run |
| `location` | `object` | 否 | App 进程内定位目标白名单。声明 CoreLocation spawn target；缺省表示该 App 不允许平台自动注入。 |
| `login` | `object` | 否 | — |
| `operations_dir` | `string` | 否 | — |
| `product_key` | `string` | 否 | 同一产品跨环境 App Profile 的稳定分组键；缺省时等同 app |
| `quick_tools` | `array<object>` | 否 | 操作库页顶部的快捷工具槽位。原先「订单快捷工具」卡按 app 名硬编码在前端，现改为声明式：每个槽位绑定一条在用 operation，presets 只是该 operation 的预置参数组合（不另起第二套执行协议）。未声明即不显示。 |
| `recipes_dir` | `string` | 否 | — |
| `require_discrimination` | `boolean` | 否 | 转正是否默认强制区分力闸（ADR-0001）。缺省 true：可执行用例（automation=ui_ready）draft→active 须附当前 build 的有牙证据。设 false 整 app 退回旧行为（rollout 逃生阀） |
| `requirement_inputs` | anyOf | 否 | 交付需求输入（需求原文 + 冻结设计包）在产品项目里的真源目录，一轮一个子目录。没有这一段就表示该 app 不把需求输入放进产品项目。 |
| `requirements` | `object` | 否 | 可选的 app 级能力底线；与 recipe requirements 合并时只能收紧，不能削弱 vm_uds/app_ui/location_injection 等安全能力。 |
| `reset` | enum: `'ui_home'` \| `'relaunch'` \| `'none'` | 否 | 每用例复位策略 |
| `schema_version` | const `'v1'` | 是 | — |
| `scripts_dir` | `string` | 否 | — |
| `store_catalog` | `object` | 否 | App 使用的门店基础数据品牌。声明后看板标头显示门店选择器，定位与工具页共享同一门店上下文。 |
| `trial_data_isolation` | enum: `'unisolated'` \| `'isolated'` | 否 | 草稿试跑数据隔离声明（D7）：缺省 unisolated。仅当显式声明 isolated 且 CLI 同时传入 --trial-draft-recipes --confirm-trial-effects 时，才允许 draft 试跑执行 lower_bound / unknown 副作用步骤。 |

## Case Registry (`cases/*.yaml`)

- Schema 文件：`autophone/_data/schemas/case-registry.v2.json`
- 说明：业务用例库 v2。Case 记录测试意图和断言；历史上写入 Case.steps 的 typed IR 暂按兼容联合类型读取，新增执行步骤应进入 Recipe/Operation。
- 必填字段：`cases`、`meta`

| 字段 | 类型 / 枚举 | 必填 | 说明 |
| --- | --- | --- | --- |
| `cases` | `array<#/$defs/case>` | 是 | — |
| `meta` | `object` | 是 | — |
| `stats` | `#/$defs/importStats` | 否 | — |

### 嵌套定义（`$defs`）

- **`case`**：必填: `id`, `item`, `revision`, `status`；字段: `automation`, `automation_reason`, `business_family`, `case_type`, `coverage`, `delivery_contract`, `deprecate_reason`, `desc`, `entity_binding`, `expects`…
- **`caseStep`**：oneOf
- **`flow`**：必填: `id`, `step`；字段: `fixture`, `id`, `produces`, `requires`, `runtime_actor_variant`, `step`
- **`importAction`**：必填: `kind`, `label`, `target`；字段: `branch`, `kind`, `label`, `target`
- **`importAssertion`**：必填: `kind`, `label`；字段: `kind`, `label`
- **`importPrecondition`**：必填: `kind`, `label`；字段: `kind`, `label`
- **`importSemantics`**：必填: `actions`, `assertions`, `input_fingerprint`, `preconditions`, `source`, `summary`, `version`；字段: `actions`, `assertions`, `input_fingerprint`, `preconditions`, `source`, `summary`, `version`
- **`importStats`**：必填: `by_automation`, `by_module`；字段: `by_automation`, `by_module`
- **`nonnegativeCounts`**：`object`
- **`operationReference`**：必填: `op`；字段: `op`, `reason`, `with`
- **`operationReferences`**：`array<#/$defs/operationReference>`
- **`stringList`**：`array<string>`

## Run Spec (`POST /runs` / `run-spec.json`)

- Schema 文件：`autophone/_data/schemas/run-spec.v1.json`
- 说明：一次 run 的输入声明（配置期）。RunResult 在执行期产生，见 run-result.v1.json。
- 必填字段：`app`、`device`、`run_id`、`schema_version`、`type`

| 字段 | 类型 / 枚举 | 必填 | 说明 |
| --- | --- | --- | --- |
| `app` | `string` | 是 | — |
| `auto_login` | `boolean` | 否 | needs_login 时是否在登录闸内自动补登（默认 false=fail-fast）。验证登录失败等需停在登出态的用例务必保持缺省/false。 |
| `autophone_version` | `string` | 否 | run 创建时冻结的 autophone 发布版本 |
| `candidate_ref` | `candidate-ref.v1.json` | 否 | — |
| `case_selector` | `object` | 否 | regression/verification 用；explore 可省略 |
| `commit` | `string` | 否 | run 创建时冻结的 autophone commit |
| `crash_trap` | `boolean` | 否 | execute 阶段是否后台挂 crash_trap 兜底捕获被测 app 的原始 SIGBUS/SIGSEGV 崩点（落 planes/crash/）。缺省=开（auto，无 frida 则 fail-open 静默跳过）；显式 false 关。也可用环境 AUTOPHONE_CRASH_TRAP=0 全局关。 |
| `device` | `string` | 是 | 默认单机；预留多 VM 维度 |
| `execution_plan` | enum: `'current'` | 否 | App-specific serial batch plan. current includes active and draft cases, excludes R104, and defers unproven duty transitions. |
| `fixture` | `object` | 否 | 前置状态声明，内容由各 App 的 fixture adapter 解释（app-specific）。 |
| `golden_path` | `object` | 否 | 黄金路径 Run 标记：本 Run 为 apps/<app>/golden_paths.yaml 中点名的黄金路径提交。提交端与执行端核对路径已声明、case_id 属于该路径、declaration_sha256 等于当前声明摘要（不含 title），且 case_selector 等于该 Case 当前生命周期下的计划 selector；仅 type=regression 有效，不得与 execution_plan、negative_control 同用。执行端对该 Case 的 live Recipe 读取一次并冻结，并绑定 Fixture Readback 计划；无此字段的普通回归行为不变。 |
| `identity_requirements` | `object` | 否 | — |
| `negative_control` | `object` | 否 | 区分力反向 Run：按 apps/<app>/discrimination/negative_controls.yaml 中点名的声明，对唯一被选 Case 的 Recipe 施加最小反向状态后执行。执行端核对声明摘要，不一致即拒绝执行；仅 type=regression 且 case_selector.ids 恰为该 Case 时有效。此类 Run 不计入 Draft Trial 转正证据与回归裁决。 |
| `operation` | `object` | 否 | type=operation 的操作引用：按 name 指向 apps/<app>/operations 的操作，with 给入参。 |
| `origin` | `object` | 否 | run 触发来源；调度/门禁等派生编排留痕 |
| `planes` | `object` | 否 | 四观测面的策略（mode），裁决（verdict）在 RunResult 里产生。 |
| `requested_at` | `string` | 否 | — |
| `requested_by` | `string` | 否 | — |
| `run_id` | `string` | 是 | — |
| `runtime_requirements` | `object` | 否 | — |
| `schema_version` | const `'v1'` | 是 | — |
| `store_context` | `#/$defs/storeContextInput` | 否 | 看板标头所选门店。提交时可只给 brand/store_code；RunService 会从门店目录重新解析并替换为不可伪造的规范快照。 |
| `trial_confirmation` | `array<object>` | 否 | — |
| `type` | enum: `'regression'` \| `'verification'` \| `'operation'` \| `'explore'` | 是 | — |
| `verification` | `object` | 否 | type=verification 的验证单声明。可引用 apps/<app>/verification 下的 yaml，也可 inline requirement/checkpoints。 |

### 嵌套定义（`$defs`）

- **`apiPlanePolicy`**：必填: `mode`；字段: `mode`, `scenario`
- **`apiScenarioRef`**：必填: `path`, `sha256`；字段: `path`, `sha256`
- **`planePolicy`**：必填: `mode`；字段: `mode`
- **`storeContextInput`**：oneOf
- **`storeContextSnapshot`**：必填: `address`, `brand`, `catalog_snapshot_id`, `city_name`, `coordinates`, `identity`, `market_code`, `market_name`, `name`, `national_city_code`, `schema_version`, `short_name`, `status`, `store_code`, `store_type`, `tolerance_m`；字段: `address`, `brand`, `catalog_snapshot_id`, `city_name`, `coordinates`, `identity`, `market_code`, `market_name`, `name`, `national_city_code`…
- **`storeCoordinate`**：oneOf

## Run Result (`platform-run-result.json`)

- Schema 文件：`autophone/_data/schemas/run-result.v1.json`
- 说明：一次 run 的执行结果（执行期）。planes 用 mode（策略）+ verdict（裁决）两层。
- 必填字段：`app`、`device`、`planes`、`run_id`、`schema_version`、`status`、`timestamps`、`type`

| 字段 | 类型 / 枚举 | 必填 | 说明 |
| --- | --- | --- | --- |
| `abort_reason` | `string` | 否 | Run 中止（status=blocked）时的中止原因原文，与引擎 result.json 的 abort_reason 相同；中止后未执行的 Case 在 cases[] 中记为 blocked 并带 blocker |
| `app` | `string` | 是 | — |
| `app_version` | `string` | 否 | run 起始时读 Info.plist 得到的被测包版本 |
| `artifacts` | `string` | 否 | 指向本 run 的 artifact-manifest.json 的相对路径（自包含 run 目录内） |
| `assignment_id` | `string` | 否 | — |
| `attempt_no` | `integer` | 否 | — |
| `autophone_version` | `string` | 否 | run 执行时的 autophone 发布版本 |
| `ax_backend` | enum: `'frida_inproc'` \| `'guest_axruntime'` \| `None` | 否 | Run 启动期从 App Profile 冻结的单一 AX 观测后端（D8）；未启用或旧 Run 可为 null 或省略。 |
| `baseline_ref` | `object` | 否 | — |
| `blocker` | `#/$defs/blocker` | 否 | Run 在执行前或执行中遇到的结构化阻塞原因；不表示产品断言失败。 |
| `build_label` | `string` | 否 | run 提交时冻结的 App Profile 构建标签；与 package.profile_build_label 同源 |
| `calibration_record` | `object` | 否 | — |
| `candidate_activation` | `candidate-activation-result.v1.json` | 否 | — |
| `captures` | `array<object>` | 否 | — |
| `cases` | `array<object>` | 否 | — |
| `commit` | `string` | 否 | run 执行时的 autophone commit |
| `device` | `string` | 是 | — |
| `env` | `string` | 否 | 如 uat / prod |
| `error` | `object` | 否 | — |
| `evidence_kind` | enum: `'device'` \| `'host_test'` | 否 | — |
| `failed_definition` | `failure-context.v1.json#/$defs/failedDefinition` | 否 | 明确定位且有版本身份的失败定义；未知时不继承外层执行对象，不表示已确认资产缺陷。 |
| `failure_reasons` | `array<any>` | 否 | status=failed 时的机器路由原因；product 与 asset 可同时存在 |
| `failure_stage` | `failure-context.v1.json#/$defs/failureStage` | 否 | — |
| `fencing` | `integer` | 否 | — |
| `groups` | `array<object>` | 否 | 分组批跑的有序执行计划与终态；普通单组 run 可省略。 |
| `identity` | `object` | 否 | — |
| `lock_phase` | `object` | 否 | 三段生命周期，仅 execute 占屏幕互斥锁 |
| `package` | anyOf | 否 | run 提交时冻结的包/构建快照；registry_build_ref 是 Host 侧选择，device_build_observation 是设备侧观测，二者的匹配状态单独记在 build_identity_match。default_* 表示 Registry 的默认选择；旧字段 active_* / device_version 仅作兼容读取输入，新代码不再写入。字段允许 null 以兼容旧 run 与离线设备 |
| `planes` | `object` | 是 | — |
| `rig` | `object` | 否 | 执行前在同一把 ScreenLock 内采集的 VM / App / Frida / 健康守护快照 |
| `run_id` | `string` | 是 | — |
| `schema_version` | const `'v1'` | 是 | — |
| `status` | enum: `'passed'` \| `'failed'` \| `'error'` \| `'blocked'` \| `'pending'` \| `'partial'` | 是 | pending：仍有 mode=required 面 verdict=pending（如 telemetry readback 中） |
| `steps` | `array<object>` | 否 | 操作回放的逐步日志（仅 type=operation/verification 时出现） |
| `store_context` | `#/$defs/storeContextSnapshot` | 否 | run 提交时由门店目录冻结的规范门店上下文。 |
| `timestamps` | `object` | 是 | — |
| `type` | enum: `'regression'` \| `'verification'` \| `'operation'` \| `'explore'` \| `'visual_calibration'` | 是 | — |
| `world_identity` | `run-world-identity.v0.json` | 否 | — |

### 嵌套定义（`$defs`）

- **`apiPlaneResult`**：必填: `mode`, `verdict`；字段: `delivery`, `detail`, `mode`, `rule_snapshot_hash`, `scenario`, `verdict`
- **`apiScenarioRef`**：必填: `path`, `sha256`；字段: `path`, `sha256`
- **`blocker`**：必填: `code`, `recovery`, `scope`；字段: `capability`, `code`, `evidence_ref`, `recovery`, `scope`
- **`jointVerdictResult`**：必填: `detail`, `entity_id`, `entity_type`, `fail_kind`, `mismatch_reasons`, `planes`, `status`；字段: `detail`, `entity_id`, `entity_type`, `fail_kind`, `mismatch_reasons`, `planes`, `status`
- **`operationIdentity`**：必填: `app`, `content_sha256`, `id`, `name`, `revision`；字段: `app`, `content_sha256`, `id`, `name`, `revision`
- **`phaseOutcome`**：字段: `ended_at`, `started_at`, `status`
- **`planeResult`**：必填: `mode`, `verdict`；字段: `ax_backend`, `detail`, `mode`, `verdict`
- **`rigEvidence`**：字段: `available`, `daemon`, `evidence`, `on_down`, `state`
- **`storeContextSnapshot`**：必填: `address`, `brand`, `catalog_snapshot_id`, `city_name`, `coordinates`, `identity`, `market_code`, `market_name`, `name`, `national_city_code`, `schema_version`, `short_name`, `status`, `store_code`, `store_type`, `tolerance_m`；字段: `address`, `brand`, `catalog_snapshot_id`, `city_name`, `coordinates`, `identity`, `market_code`, `market_name`, `name`, `national_city_code`…
- **`storeCoordinate`**：oneOf
