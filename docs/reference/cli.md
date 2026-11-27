# CLI 命令参考（自动生成）

> 本文档由 `scripts/generate_public_docs.py` 从公开核心态 `autophone.cli.build_parser(include_extensions=False)` 自动生成，请勿手工修改。
> 默认仅列出公开核心主命令；如需在 `autophone --help` 中查看实验性子命令，可设置 `AUTOPHONE_CLI_SHOW_EXPERIMENTAL=1`。

## 命令索引

| 命令 | 说明 |
| --- | --- |
| `autophone ai` | call local agent CLI (claude/codex) for non-deterministic work |
| `autophone ai ask` | one-off prompt to the agent (text out) |
| `autophone ai convert` | convert an NL case into a draft typed-IR recipe (AI-5, P1 转换器脊) |
| `autophone ai convert-material` | 降解集成用例 xlsx 一行 → case+api_expectations+fixtures 三份草稿（P2，§六） |
| `autophone ai diagnose` | AI failure attribution over a run's sanitized artifacts (AI-1) |
| `autophone ai doctor` | CLI capability detection (presence/version/default provider) |
| `autophone ai draft-operation` | draft a reusable operation recipe into the registry as status=draft (AI-4) |
| `autophone ai propose-cases` | propose candidate cases from free-form requirements for human curation |
| `autophone ai pull-extraction` | auto-pull: convert a case, auto-draft the missing precondition operation on needs_extraction (AI-6, P3 抽取环) |
| `autophone ai report` | best-effort agent: read-only screen report, zero side-effects (AI-8, P5 Lane 3) |
| `autophone ai suggest-ops` | recommend operation-library ops for a case's preconditions/post-actions into suggested_setup/teardown (待确认) |
| `autophone ai task` | best-effort agent: plan a one-off task, pause before irreversible/outward actions (AI-8, P5 Lane 2) |
| `autophone ai verify-agent` | verification AGENT_CMD adapter: drive device per checkpoint (AI-2) |
| `autophone app` | manage app profiles, packages, and cases |
| `autophone app add` | register a new app profile (scaffolds apps/<app>/) |
| `autophone app case` | case library: import from the generic xlsx template |
| `autophone app case import` | import cases from the general case-template xlsx layout (seed or merge) |
| `autophone app case lint` | validate Case Registry v2 and active/ui_ready AssetAdmission (CI gate) |
| `autophone app list` | — |
| `autophone app package` | ordered IPA store: upload + install builds |
| `autophone app package add` | ingest an IPA into the host store |
| `autophone app package default` | set the default slot without changing any Rig |
| `autophone app package install` | upload + install one Build into the VM (default: installed, else newest candidate) |
| `autophone app package list` | list Version Slots and their Build history |
| `autophone app package migrate` | package store v1 → v2 迁移：默认只读预览，--apply 才写入 |
| `autophone app package migrate-manifest` | backfill short/build version fields from registered IPA files |
| `autophone app package promote` | 将现有 Build 显式晋级为 Version Slot Candidate |
| `autophone app package prune` | 删除 IPA 字节并保留 Build Record |
| `autophone app package relocate` | 校验并复制 Build 产物到另一个 Artifact Store |
| `autophone app package remove` | drop a slot from the host store |
| `autophone app show` | — |
| `autophone app symbols` | manage immutable app-symbols.v1 bindings (L2 manifest import & L1 RN extraction) |
| `autophone app symbols extract-l1-rn` | blind-extract React Native main.jsbundle identifiers from an unencrypted IPA |
| `autophone app symbols import-l2` | import a build-side L2 thin symbol manifest and emit static_hint claims |
| `autophone control` | PostgreSQL Control Plane client |
| `autophone control access` | B 端接入授权记录（access_grants）与预留对账 |
| `autophone control access list` | — |
| `autophone control access reconcile` | — |
| `autophone control assignment` | — |
| `autophone control assignment cancel` | — |
| `autophone control assignment get` | — |
| `autophone control assignment rerun` | submit a new Assignment with a fresh run_id; the first attempt's evidence stays |
| `autophone control assignment retry` | — |
| `autophone control assignment submit` | — |
| `autophone control evidence` | — |
| `autophone control evidence approve` | bind an operator review to capacity_qualification evidence |
| `autophone control evidence validate` | — |
| `autophone control host` | — |
| `autophone control host admission-mode` | — |
| `autophone control host admission-shadow` | — |
| `autophone control host allocatable` | — |
| `autophone control host anomalies` | — |
| `autophone control host anomalies list` | — |
| `autophone control host anomalies report` | — |
| `autophone control host anomalies review` | — |
| `autophone control host anomalies show` | — |
| `autophone control host breaker` | — |
| `autophone control host capacity` | — |
| `autophone control host get` | — |
| `autophone control host maintenance` | — |
| `autophone control host maintenance complete` | — |
| `autophone control host maintenance start` | — |
| `autophone control recovery` | — |
| `autophone control recovery confirm-isolation` | — |
| `autophone control recovery deliver` | — |
| `autophone control recovery get` | — |
| `autophone control recovery list` | 只读列出一个 Rig 的 recovery incident |
| `autophone control recovery resume` | — |
| `autophone control reservation` | — |
| `autophone control reservation create` | — |
| `autophone control reservation release` | — |
| `autophone control rig` | — |
| `autophone control rig deregister` | — |
| `autophone control rig list` | — |
| `autophone control rig recover` | — |
| `autophone control runs` | — |
| `autophone control runs cohort-freeze` | freeze a runnability cohort (cases, selector, asset sha256, planned run ids) |
| `autophone control runs reconcile` | list or converge runs rows left at queued/assigned behind a terminal Assignment |
| `autophone control runs runnability` | read-only eligible/started/passed/blocked counts for a frozen cohort |
| `autophone device` | 设备级本机状态（ScreenLock） |
| `autophone device screenlock` | ScreenLock 隔离标记 |
| `autophone device screenlock release-quarantine` | 核对前置与设备探针后归档隔离标记并写解除回执；不带 --confirm-quiesced 时只核对 |
| `autophone device screenlock status` | 只读：锁状态与隔离标记 |
| `autophone doctor` | check VM, UDS, API, Frida and optional helper readiness |
| `autophone location` | manage a Rig's system location through the dashboard |
| `autophone location current` | — |
| `autophone location route` | — |
| `autophone location set` | — |
| `autophone location stop` | — |
| `autophone operation` | trigger a type=operation run (operations registry) |
| `autophone operation review` | 给一条 operation run 落人审结论（转正闸要 approved） |
| `autophone operation run` | run one registered operation by name |
| `autophone record` | recording asset compilation |
| `autophone record case-recipe` | inspect or compile explicit Case coverage |
| `autophone record native` | passively record pointer input in the real VPHONE window |
| `autophone record window-probe` | read real VPHONE content geometry without device input |
| `autophone replay` | replay a single regression case by id |
| `autophone report` | locate platform run reports |
| `autophone rig` | initialize and bring up local upstream Rigs |
| `autophone rig init` | generate a validated upstream_v1 DeviceTarget JSON for one VM |
| `autophone rig up` | start VMs, Control Plane (or local single-VM mode), Rig Agents and the dashboard |
| `autophone run` | run deterministic regression cases |
| `autophone schedule` | single-process cron schedules for platform runs |
| `autophone schedule disable` | — |
| `autophone schedule enable` | — |
| `autophone schedule fire` | — |
| `autophone schedule list` | — |
| `autophone screens` | screen skeleton assets and checkpoint telemetry |
| `autophone screens draft` | draft screens/<build>.yaml from OCR samples JSON |
| `autophone screens telemetry` | summarize operation screen_checkpoint telemetry |
| `autophone screens validate` | validate one screens/<build>.yaml asset |
| `autophone self-update` | managed release install, upgrade and rollback |
| `autophone self-update apply` | — |
| `autophone self-update check` | — |
| `autophone self-update recover` | — |
| `autophone self-update rollback` | — |
| `autophone self-update stage` | — |
| `autophone self-update status` | — |
| `autophone serve` | start the web dashboard (FastAPI on /) |

## 命令参数详情

### `autophone`

Operate and manage vphone-cli iOS virtual machines

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--version` | 否 | — | — | show the build version and exit |
| `-v`, `--verbose` | 否 | — | `0` | 日志级别：默认 INFO，-v 起 DEBUG(更详细的动作/中间态) |
| `--app` | 否 | — | `None` | app id; defaults to the only installed app profile |
| `--device` | 否 | — | `'vm'` | — |
| `--out-root` | 否 | — | `None` | — |
| `--requested-by` | 否 | — | `'local'` | — |
| `--run-id` | 否 | — | `None` | — |

### `autophone ai`

call local agent CLI (claude/codex) for non-deterministic work

### `autophone ai ask`

one-off prompt to the agent (text out)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<prompt>` | 是 | — | — | — |
| `--out-root` | 否 | — | — | — |
| `--device` | 否 | — | `False` | drive device via vphone_mcp under ScreenLock |
| `--screenshot` | 否 | — | `False` | explicitly consent to capture and send this request's current-screen snapshot; capture failure aborts |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |
| `--timeout` | 否 | — | `300.0` | — |
| `--context-run` | 否 | — | `None` | feed a run's sanitized ai-context bundle (§5.2) |
| `--allow-raw` | 否 | — | `False` | DANGER: include raw/forbidden artifacts in the AI context |

### `autophone ai convert`

convert an NL case into a draft typed-IR recipe (AI-5, P1 转换器脊)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app>` | 是 | — | — | — |
| `<case_id>` | 是 | — | — | case id to convert, e.g. R47 |
| `--note` | 否 | — | `None` | extra provenance note |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |

### `autophone ai convert-material`

降解集成用例 xlsx 一行 → case+api_expectations+fixtures 三份草稿（P2，§六）

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app>` | 是 | — | — | — |
| `<xlsx>` | 是 | — | — | 集成用例 xlsx（营销/点餐模块） |
| `--row` | 否 | — | `None` | 只降解第 N 行（0 起） |
| `--offset` | 否 | — | `0` | 从第 N 行开始批量降解（0 起；与 --limit 配合分批） |
| `--limit` | 否 | — | `None` | 只降解前 N 行 |
| `--dry-run-summary` | 否 | — | `False` | 逐行降解到临时资产，只输出分箱统计，不写真实 cases/api_expectations/fixtures |
| `--note` | 否 | — | `None` | extra provenance note |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |
| `--retries` | 否 | — | `1` | 单行瞬态失败（超时/截断 JSON/schema 漂移）重打次数（默认 1；0=不重试） |
| `--resume` | 否 | — | `False` | 断点续跑：跳过用例库里 raw.external_id 已存在的行，避免重复追加草稿 |
| `--exclude-order-payment` | 否 | — | `False` | 按来源标题/步骤/API 静态排除支付或下单路径；排除行不发送给 AI、不写入资产 |

### `autophone ai diagnose`

AI failure attribution over a run's sanitized artifacts (AI-1)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<run_id>` | 是 | — | — | — |
| `--out-root` | 否 | — | — | — |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |
| `--allow-raw` | 否 | — | `False` | DANGER: include raw screenshots/captures in the diagnosis context |
| `--no-images` | 否 | — | `False` | 不随 prompt 附带净化包里 ai_policy 放行的截图（治理判定不变） |

### `autophone ai doctor`

CLI capability detection (presence/version/default provider)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--probe` | 否 | — | `False` | run one tiny real call to prove the bridge |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--timeout` | 否 | — | `30.0` | — |

### `autophone ai draft-operation`

draft a reusable operation recipe into the registry as status=draft (AI-4)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app>` | 是 | — | — | — |
| `<requirement>` | 是 | — | — | what the operation should accomplish |
| `--from-file` | 否 | — | `None` | conversation trace / exploration notes file to distill steps from |
| `--from-ref` | 否 | — | `None` | provenance.from: source conversation/session id |
| `--note` | 否 | — | `None` | extra provenance note |
| `--run-id` | 否 | — | `None` | provenance.run_id if distilled from a run |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |

### `autophone ai propose-cases`

propose candidate cases from free-form requirements for human curation

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app>` | 是 | — | — | — |
| `<requirement>` | 否 | — | — | free-form requirement text (or use --from-file) |
| `--from-file` | 否 | — | `None` | requirements doc / digest file to propose from |
| `--max-candidates` | 否 | — | `10` | — |
| `--commit` | 否 | — | `False` | write proposed candidates into the case library as status=draft (human then curates by activating) |
| `--note` | 否 | — | `None` | extra provenance note |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |

### `autophone ai pull-extraction`

auto-pull: convert a case, auto-draft the missing precondition operation on needs_extraction (AI-6, P3 抽取环)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app>` | 是 | — | — | — |
| `<case_id>` | 是 | — | — | case id to convert + pull extraction for, e.g. R80 |
| `--max-draft-attempts` | 否 | — | `2` | auto-draft 连续失败几次后升 stall（默认 2） |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |

### `autophone ai report`

best-effort agent: read-only screen report, zero side-effects (AI-8, P5 Lane 3)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<prompt>` | 是 | — | — | 要看/汇报什么（如「今天有哪些待处理订单」） |
| `--device` | 否 | — | `False` | 经 vphone_mcp 给设备工具面（只读约束靠契约，碰屏仍进 ScreenLock）；否则喂当前屏纯推理 |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |
| `--timeout` | 否 | — | `300.0` | — |

### `autophone ai suggest-ops`

recommend operation-library ops for a case's preconditions/post-actions into suggested_setup/teardown (待确认)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app>` | 是 | — | — | — |
| `--case` | 否 | — | `None` | 单条用例 id（如 R47）；省略则按 --status 批量 |
| `--status` | 否 | — | `'draft'` | 批量模式只处理该状态的用例（默认 draft） |
| `--dry-run` | 否 | — | `False` | 只算不写回 suggested_* |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |

### `autophone ai task`

best-effort agent: plan a one-off task, pause before irreversible/outward actions (AI-8, P5 Lane 2)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<task>` | 是 | — | — | 自然语言任务（如「给张三发条消息」） |
| `--app` | 否 | — | `None` | 被测 app id（有 profile 才算「已知」；未知/首次走整盘预批） |
| `--familiar` | 否 | — | `None` | 人工强制「熟悉」（默认不带 = 从当前构建的成功驱动史证据派生，见 familiarity.py） |
| `--unfamiliar` | 否 | — | `True` | 人工强制「不熟悉」→ 整盘预批（覆写证据派生） |
| `--approved` | 否 | — | `False` | 人已就上轮 plan 批准 → 放行执行（含不可逆动作） |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |
| `--timeout` | 否 | — | `300.0` | — |

### `autophone ai verify-agent`

verification AGENT_CMD adapter: drive device per checkpoint (AI-2)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--input` | 否 | — | `None` | verification-agent-input.json (default: $AUTOPHONE_VERIFICATION_INPUT) |
| `--output` | 否 | — | `None` | output path (default: $AUTOPHONE_VERIFICATION_OUTPUT) |
| `--provider` | 否 | `claude`, `codex`, `auto` | `None` | — |
| `--model` | 否 | — | `None` | — |

### `autophone app`

manage app profiles, packages, and cases

### `autophone app add`

register a new app profile (scaffolds apps/<app>/)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | app id / 目录名（小写 slug） |
| `--bundle-id` | 是 | — | `None` | — |
| `--display-name` | 是 | — | `None` | — |
| `--env` | 否 | — | `None` | — |
| `--login` | 否 | `script`, `manual` | `'manual'` | — |
| `--reset` | 否 | `ui_home`, `relaunch`, `none` | `'ui_home'` | — |

### `autophone app case`

case library: import from the generic xlsx template

### `autophone app case import`

import cases from the general case-template xlsx layout (seed or merge)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `<xlsx>` | 是 | — | — | 通用用例模板 xlsx 路径 |
| `--dry-run` | 否 | — | `False` | 只打将写/追加的条数，不落盘 |

### `autophone app case lint`

validate Case Registry v2 and active/ui_ready AssetAdmission (CI gate)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 否 | — | — | app id; omit to audit every app under apps/ |
| `--build-id` | 否 | — | `None` | Package Store build_id for resolving app-symbols.v1 bindings (defaults to promoted build) |

### `autophone app list`

### `autophone app package`

ordered IPA store: upload + install builds

### `autophone app package add`

ingest an IPA into the host store

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `<ipa>` | 是 | — | — | — |
| `--label` | 否 | — | `None` | human label / build name (default: version or filename) |
| `--force` | 否 | — | `False` | import even if the app is unknown or the IPA bundle id mismatches the profile |
| `--commit` | 否 | — | `None` | 产生该 Build 的完整产品源码 commit（干净工作树） |
| `--source-tree-sha256` | 否 | — | `None` | dirty/generated 源码树摘要 |
| `--base-commit` | 否 | — | `None` | --source-tree-sha256 的基线 commit |
| `--source-state` | 否 | `dirty`, `generated` | `None` | 源码树状态（默认 dirty） |
| `--artifact-store` | 否 | — | `None` | 产物存储 ID；缺省使用 App Profile 配置，未配置时使用 central；复用已有 Blob 时显式冲突会拒绝入库 |
| `--retention-class` | 否 | `iteration`, `candidate`, `accepted`, `baseline` | `None` | 产物保留分类 |
| `--no-candidate` | 否 | — | `False` | 只登记迭代 Build，不替换当前 Candidate |

### `autophone app package default`

set the default slot without changing any Rig

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `<slot>` | 是 | — | — | — |

### `autophone app package install`

upload + install one Build into the VM (default: installed, else newest candidate)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `--slot` | 否 | — | `None` | version slot；不带 --build-id 时解析该 slot 的 Candidate |
| `--build-id` | 否 | — | `None` | 精确 Build（自动化调用方必须用这个） |
| `--no-verify` | 否 | — | `False` | skip the post-install LaunchServices registration check |
| `--rig-id` | 否 | — | `None` | target Rig when Control Plane is configured |
| `--device` | 否 | — | `None` | 目标设备（VM 目录名或 rig_id）；控制面已配置且未给 --rig-id 时必填，按设备解析唯一 Rig 固定安装目标（BL-218） |
| `--priority` | 否 | `high`, `normal` | `'normal'` | — |
| `--idempotency-key` | 否 | — | `None` | — |

### `autophone app package list`

list Version Slots and their Build history

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |

### `autophone app package migrate`

package store v1 → v2 迁移：默认只读预览，--apply 才写入

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `--apply` | 否 | — | `False` | 写入 v2 manifest 与 Target 级回执 |
| `--exclude-artifact` | 否 | — | `None` | 排除裁定：该 Artifact 退出来源冲突门，Source Revision 保持 unknown；可重复 |
| `--exclusion-reason` | 否 | — | `None` | --exclude-artifact 的裁定理由，必填 |
| `--decided-by` | 否 | — | `None` | --exclude-artifact 的裁定人，必填 |
| `--decided-at` | 否 | — | `None` | 裁定时间；默认取当前时间 |
| `--decision-file` | 否 | — | `None` | 裁定记录 JSON：{"decided_by","decided_at","exclusions":[…]} |

### `autophone app package migrate-manifest`

backfill short/build version fields from registered IPA files

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |

### `autophone app package promote`

将现有 Build 显式晋级为 Version Slot Candidate

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `--build-id` | 是 | — | `None` | — |

### `autophone app package prune`

删除 IPA 字节并保留 Build Record

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `--build-id` | 是 | — | `None` | — |
| `--force` | 否 | — | `False` | 忽略 installed/Candidate/accepted/baseline 保留闸 |

### `autophone app package relocate`

校验并复制 Build 产物到另一个 Artifact Store

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `--build-id` | 是 | — | `None` | — |
| `--artifact-store` | 是 | — | `None` | — |

### `autophone app package remove`

drop a slot from the host store

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |
| `<slot>` | 否 | — | — | slot（兼容入口，删该 slot 的 Candidate） |
| `--build-id` | 否 | — | `None` | 精确 Build |
| `--force` | 否 | — | `False` | 忽略保留闸（installed / candidate / 已接受需求引用） |

### `autophone app show`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | — |

### `autophone app symbols`

manage immutable app-symbols.v1 bindings (L2 manifest import & L1 RN extraction)

### `autophone app symbols extract-l1-rn`

blind-extract React Native main.jsbundle identifiers from an unencrypted IPA

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | app id / 目录名 |
| `--ipa` | 是 | — | `None` | 未加密 React Native IPA 路径 |
| `--build-id` | 否 | — | `None` | 显式 Build ID（省略时按 IPA SHA-256 派生 bld-<sha[:12]>） |
| `--default-screen-id` | 否 | — | `'UNASSIGNED'` | 提取候选默认归属的 screen_id（默认 UNASSIGNED） |
| `--symbol-prefix` | 否 | — | `'ctrl.rn'` | 派生逻辑 symbol_id 前缀（默认 ctrl.rn） |
| `--output-root` | 否 | — | `None` | 输出资产根目录（默认写入当前 App 资产根 apps/<app>/symbols/） |
| `--overwrite` | 否 | — | `False` | 允许覆盖同制品摘要下内容有变更的既有符号文件 |
| `--json` | 否 | — | `False` | 以 JSON 格式输出结构化摘要 |

### `autophone app symbols import-l2`

import a build-side L2 thin symbol manifest and emit static_hint claims

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<app_name>` | 是 | — | — | app id / 目录名 |
| `--manifest` | 是 | — | `None` | 构建侧 L2 薄清单 JSON/YAML 文件路径 |
| `--build-id` | 否 | — | `None` | Package Store 精确 Build ID（缺省时使用清单内 artifact_identity） |
| `--output-root` | 否 | — | `None` | 输出资产根目录（默认写入当前 App 资产根 apps/<app>/symbols/） |
| `--package-root` | 否 | — | `None` | Package Store 根目录（默认 STATE_ROOT） |
| `--overwrite` | 否 | — | `False` | 允许覆盖同制品摘要下内容有变更的既有符号文件 |
| `--json` | 否 | — | `False` | 以 JSON 格式输出结构化摘要 |

### `autophone control`

PostgreSQL Control Plane client

### `autophone control access`

B 端接入授权记录（access_grants）与预留对账

### `autophone control access list`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--status` | 否 | `creating`, `active`, `closing`, `closed` | `None` | 只列这些状态（可重复） |

### `autophone control access reconcile`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--device` | 否 | — | `None` | 额外检查的设备键（可重复） |
| `--actor` | 否 | — | `'cli'` | — |
| `--dry-run` | 否 | — | `False` | — |

### `autophone control assignment`

### `autophone control assignment cancel`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<assignment_id>` | 是 | — | — | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |

### `autophone control assignment get`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<assignment_id>` | 是 | — | — | — |

### `autophone control assignment rerun`

submit a new Assignment with a fresh run_id; the first attempt's evidence stays

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<assignment_id>` | 是 | — | — | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |

### `autophone control assignment retry`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<assignment_id>` | 是 | — | — | — |

### `autophone control assignment submit`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<plan>` | 是 | — | — | — |
| `--idempotency-key` | 是 | — | `None` | — |

### `autophone control evidence`

### `autophone control evidence approve`

bind an operator review to capacity_qualification evidence

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<report>` | 是 | — | — | — |
| `--output` | 是 | — | `None` | — |
| `--evidence-root` | 否 | — | `None` | — |
| `--reviewer` | 是 | — | `None` | — |
| `--reviewed-at` | 否 | — | `None` | — |
| `--note` | 是 | — | `None` | review rationale; approval attests sustained single-Rig baselines, the dual-Rig long window, health/fatal/USB diagnostics, and 62078 callers |

### `autophone control evidence validate`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<report>` | 是 | — | — | — |
| `--output` | 否 | — | `None` | — |
| `--evidence-root` | 否 | — | `None` | — |

### `autophone control host`

### `autophone control host admission-mode`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `<mode>` | 是 | `capacity`, `resource` | — | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |

### `autophone control host admission-shadow`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `--since` | 否 | — | `None` | ISO 8601 time |

### `autophone control host allocatable`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `--memory-gb` | 是 | — | `None` | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |

### `autophone control host anomalies`

### `autophone control host anomalies list`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `--kind` | 否 | — | `None` | — |
| `--since` | 否 | — | `None` | ISO 8601 time |
| `--limit` | 否 | — | `100` | — |

### `autophone control host anomalies report`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `--since` | 否 | — | `None` | ISO 8601 time |
| `--include-test` | 否 | — | `False` | — |

### `autophone control host anomalies review`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<anomaly_id>` | 是 | — | — | — |
| `--actor` | 是 | — | `None` | — |
| `--note` | 是 | — | `None` | — |
| `--label` | 否 | — | `[]` | — |

### `autophone control host anomalies show`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<anomaly_id>` | 是 | — | — | — |

### `autophone control host breaker`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `--reset` | 否 | — | `False` | — |
| `--actor` | 否 | — | `None` | — |
| `--reason` | 否 | — | `None` | — |

### `autophone control host capacity`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `<capacity>` | 是 | — | — | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |
| `--evidence` | 否 | — | `None` | — |
| `--approval` | 否 | — | `None` | operator review Artifact required for configured Capacity above 1 |
| `--evidence-root` | 否 | — | `None` | trusted root for local file:// evidence when raising Capacity; defaults to AUTOPHONE_EVIDENCE_ROOT |

### `autophone control host get`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |

### `autophone control host maintenance`

### `autophone control host maintenance complete`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<maintenance_id>` | 是 | — | — | — |
| `--owner` | 是 | — | `None` | — |
| `--result` | 是 | — | `None` | — |

### `autophone control host maintenance start`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<host_id>` | 是 | — | — | — |
| `--intent` | 是 | — | `None` | — |
| `--owner` | 是 | — | `None` | — |
| `--ttl-seconds` | 否 | — | `300` | — |

### `autophone control recovery`

### `autophone control recovery confirm-isolation`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<recovery_id>` | 是 | — | — | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |

### `autophone control recovery deliver`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<observation>` | 是 | — | — | — |

### `autophone control recovery get`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<recovery_id>` | 是 | — | — | — |

### `autophone control recovery list`

只读列出一个 Rig 的 recovery incident

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--rig-id` | 是 | — | `None` | — |
| `--active` | 否 | — | `False` | 只列未关闭的 incident（state 不是 recovered/isolation_confirmed） |

### `autophone control recovery resume`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<recovery_id>` | 是 | — | — | — |
| `--retry` | 否 | — | `False` | — |

### `autophone control reservation`

### `autophone control reservation create`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<spec>` | 是 | — | — | — |

### `autophone control reservation release`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<reservation_id>` | 是 | — | — | — |
| `--actor` | 是 | — | `None` | — |

### `autophone control rig`

### `autophone control rig deregister`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<rig_id>` | 是 | — | — | — |
| `--rig-generation` | 是 | — | `None` | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |

### `autophone control rig list`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--all` | 否 | — | `False` | include deregistered Rigs |

### `autophone control rig recover`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<rig_id>` | 是 | — | — | — |
| `--rig-generation` | 是 | — | `None` | — |
| `--actor` | 是 | — | `None` | — |
| `--probe-result` | 是 | — | `None` | — |
| `--wait-seconds` | 否 | — | `0.0` | 有被中断 Attempt 时等待闭包恢复 Assignment 到终态的秒数；未通过或超时退出码 3（BL-076） |

### `autophone control runs`

### `autophone control runs cohort-freeze`

freeze a runnability cohort (cases, selector, asset sha256, planned run ids)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<spec>` | 是 | — | — | cohort freeze spec JSON |
| `--out` | 否 | — | `None` | — |

### `autophone control runs reconcile`

list or converge runs rows left at queued/assigned behind a terminal Assignment

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--apply` | 否 | — | `False` | write the change (default: dry-run) |
| `--actor` | 否 | — | `None` | — |

### `autophone control runs runnability`

read-only eligible/started/passed/blocked counts for a frozen cohort

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--cohort` | 是 | — | `None` | — |
| `--source` | 否 | `pg`, `sqlite` | `'pg'` | — |
| `--run-root` | 否 | — | `None` | run root holding index.sqlite for --source sqlite (default: <state root>/runs/platform) |

### `autophone device`

设备级本机状态（ScreenLock）

### `autophone device screenlock`

ScreenLock 隔离标记

### `autophone device screenlock release-quarantine`

核对前置与设备探针后归档隔离标记并写解除回执；不带 --confirm-quiesced 时只核对

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--device` | 是 | — | `None` | — |
| `--actor` | 是 | — | `None` | — |
| `--reason` | 是 | — | `None` | — |
| `--confirm-quiesced` | 否 | — | `False` | 操作者确认设备上没有残留 Frida 会话或挂起进程；回执记为 operator_attested |
| `--frida-host` | 否 | — | `None` | host:port，只用于 Frida 只枚举探针；默认取 /tmp/autophone-rigs/<device>/device-target.json |
| `--socket` | 否 | — | `None` | UDS socket，用于锁屏状态辅助程序与 app_list 探针；默认同上 |

### `autophone device screenlock status`

只读：锁状态与隔离标记

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--device` | 是 | — | `None` | — |

### `autophone doctor`

check VM, UDS, API, Frida and optional helper readiness

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--sock` | 否 | — | `None` | — |
| `--timeout` | 否 | — | `8.0` | — |
| `--fast` | 否 | — | `False` | bounded environment gate: VM/UDS/API/Frida enumeration; never launches or restarts an App |
| `--human` | 否 | — | `False` | print human-readable tiered readiness lines instead of JSON |
| `--api-base` | 否 | — | `'http://127.0.0.1:8000'` | control-plane base; AP_API_AUTH supplies one Basic Auth credential |

### `autophone location`

manage a Rig's system location through the dashboard

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--dashboard` | 否 | — | `'http://127.0.0.1:8000'` | — |

### `autophone location current`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--rig` | 是 | — | `None` | — |

### `autophone location route`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--rig` | 是 | — | `None` | — |
| `--owner` | 是 | — | `None` | — |
| `--session-id` | 否 | — | `None` | — |
| `<route_id>` | 是 | — | — | built-in route ID |

### `autophone location set`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--rig` | 是 | — | `None` | — |
| `--owner` | 是 | — | `None` | — |
| `--session-id` | 否 | — | `None` | — |
| `--latitude` | 是 | — | `None` | — |
| `--longitude` | 是 | — | `None` | — |

### `autophone location stop`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--rig` | 是 | — | `None` | — |
| `--owner` | 是 | — | `None` | — |
| `--session-id` | 是 | — | `None` | — |

### `autophone operation`

trigger a type=operation run (operations registry)

### `autophone operation review`

给一条 operation run 落人审结论（转正闸要 approved）

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<run_id>` | 是 | — | — | — |
| `--out-root` | 否 | — | — | — |
| `--status` | 是 | `approved`, `rejected`, `needs_changes` | `None` | — |
| `--reviewer` | 否 | — | `'local'` | — |
| `--note` | 否 | — | `''` | — |

### `autophone operation run`

run one registered operation by name

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<name>` | 是 | — | — | 操作库里的稳定语义名，如 login / create_order |
| `--api` | 否 | `disabled`, `advisory` | `'disabled'` | capture API evidence through the existing RunService capture scope |
| `--with` | 否 | — | `None` | 操作入参，JSON 对象（如 '{"mode":"dry_run"}'）；由操作 params 契约校验 |
| `--draft-id` | 否 | — | `None` | 转正漏斗里被验证的 operation draft id（如 OP12）；跑 draft 回放时带上做归因统计 |
| `--out-root` | 否 | — | — | — |

### `autophone record`

recording asset compilation

### `autophone record case-recipe`

inspect or compile explicit Case coverage

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<recording_id>` | 是 | — | — | — |
| `--bindings` | 否 | — | `None` | explicit step and expectation bindings JSON |
| `--save` | 否 | — | `False` | save a new draft; default only previews |

### `autophone record native`

passively record pointer input in the real VPHONE window

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--device` | 是 | — | `None` | Control Plane rig id |
| `--starts-with` | 否 | — | `None` | Operation executed before observation starts |
| `--build` | 否 | — | `None` | recorded build label; defaults to App profile |
| `--duration` | 否 | — | `0` | seconds to observe; 0 waits for Ctrl+C |

### `autophone record window-probe`

read real VPHONE content geometry without device input

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--config` | 是 | — | `None` | exact VM config.plist |
| `--window-id` | 否 | — | `None` | optional exact CGWindowID |

### `autophone replay`

replay a single regression case by id

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--out-root` | 否 | — | — | — |
| `<case_id>` | 是 | — | — | — |
| `--record` | 否 | — | `False` | — |
| `--no-auto-login` | 否 | — | `True` | needs_login 时不自动补登（缺省补登） |

### `autophone report`

locate platform run reports

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--out-root` | 否 | — | — | — |
| `--last` | 否 | — | `False` | — |

### `autophone rig`

initialize and bring up local upstream Rigs

### `autophone rig init`

generate a validated upstream_v1 DeviceTarget JSON for one VM

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<rig_id>` | 是 | — | — | Rig identifier (typically the VM directory name under VPHONE_LIBRARY_ROOT) |
| `--bundle` | 是 | — | `None` | path to VPhone.bundle |
| `--vm-dir` | 否 | — | `None` | override VM directory (default: $VPHONE_LIBRARY_ROOT/<rig_id>) |
| `--out` | 否 | — | `None` | output target.json path (default: <vm_dir>/autophone/target.json) |
| `--host-id` | 否 | — | `None` | host identifier (default: $AUTOPHONE_HOST_ID or local hostname) |
| `--udid` | 否 | — | `None` | override expected USB UDID (default: read from <vm_dir>/udid-prediction.txt) |
| `--slot-index` | 否 | — | `None` | explicit port offset N (ssh=2222+N, frida=27042+N) |
| `--ssh-port` | 否 | — | `None` | override host SSH tunnel port |
| `--frida-port` | 否 | — | `None` | override host Frida tunnel port |
| `--frida-guest-port` | 否 | — | `27042` | guest Frida port (default: 27042) |
| `--rig-generation` | 否 | — | `1` | Rig generation number (default: 1) |

### `autophone rig up`

start VMs, Control Plane (or local single-VM mode), Rig Agents and the dashboard

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--target` | 是 | — | `None` | — |
| `--allow-multi-vm` | 否 | — | `False` | allow the selected VMs to start alongside other running VMs |
| `--ephemeral-db` | 否 | — | `False` | start a disposable local Unix-socket PostgreSQL instance when AUTOPHONE_DATABASE_URL is unset |
| `--control-port` | 否 | — | `8100` | — |
| `--dashboard-port` | 否 | — | `8000` | — |
| `--springboard-region-probe` | 否 | — | `None` | stage a trusted iOS SpringBoard region probe on each upstream Rig |
| `--decline-usb-trust` | 否 | — | `False` | check and decline iOS USB trust prompts via the managed console once per guest boot (also enabled per Rig by <runtime_dir>/decline-usb-trust) |
| `--usb-trust-wait` | 否 | — | `25.0` | seconds to watch for an iOS USB trust prompt when USB trust dismissal is enabled (0-60) |

### `autophone run`

run deterministic regression cases

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--out-root` | 否 | — | — | — |
| `--tag` | 否 | — | `None` | — |
| `--ids` | 否 | — | `None` | — |
| `--draft-cases` | 否 | — | `False` | 显式点名试跑 status=draft 的用例；必须与 --ids 同用，不进入默认回归面 |
| `--trial-draft-recipes` | 否 | — | `False` | 显式点名试跑 active case 的 recipes/drafts/<id>.yaml（不进入默认回归面） |
| `--confirm-trial-effects` | 否 | — | `None` | 确认所给准备卡 JSON 的试跑副作用；每个 Case 一个文件，可重复；资产变更后须重新读取并确认 |
| `--golden-path` | 否 | — | `None` | 按 golden_paths.yaml 中点名的黄金路径提交：写入 run-spec.golden_path 标记，缺省 selector 取该 Case 当前生命周期下的计划 selector；只有带标记的 Run 为 live Recipe 用例绑定 Fixture Readback |
| `--execution-plan` | 否 | `current` | `None` | App 注册的当前批跑编排（apps/<app>/execution_plan.py）；按 App 声明的分组顺序执行 |
| `--api` | 否 | `required`, `advisory` | `None` | 开 API 抓取面（缺省关）：required=API 断言 fail 拖垮 overall；advisory=只留证据不改判 |
| `--fixture-order` | 否 | — | `None` | 显式冻结本 run 的 fixture.order JSON；用于把已证明的 orderNum/order_id/type 注入用例 |
| `--no-auto-login` | 否 | — | `True` | needs_login 时不自动补登（缺省补登；验证登录失败等需停在登出态的用例加这个） |

### `autophone schedule`

single-process cron schedules for platform runs

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--config` | 否 | — | `<ROOT>/config/schedules.yaml` | — |
| `--out-root` | 否 | — | — | — |

### `autophone schedule disable`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<schedule_id>` | 是 | — | — | — |

### `autophone schedule enable`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<schedule_id>` | 是 | — | — | — |

### `autophone schedule fire`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<schedule_id>` | 是 | — | — | — |

### `autophone schedule list`

### `autophone screens`

screen skeleton assets and checkpoint telemetry

### `autophone screens draft`

draft screens/<build>.yaml from OCR samples JSON

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<samples>` | 是 | — | — | JSON file: {samples:[{screen_id,ocr}]} or a samples array |
| `--build` | 是 | — | `None` | — |
| `--out` | 否 | — | `None` | — |
| `--overwrite` | 否 | — | `False` | — |
| `--classify-ai` | 否 | — | `False` | classify raw OCR samples with AI before deterministic draft generation |
| `--provider` | 否 | — | `None` | — |
| `--model` | 否 | — | `None` | — |
| `--timeout-s` | 否 | — | `120.0` | — |
| `--min-samples` | 否 | — | `1` | — |
| `--must-threshold` | 否 | — | `1.0` | — |
| `--any-threshold` | 否 | — | `0.5` | — |
| `--forbid-threshold` | 否 | — | `1.0` | — |
| `--any-min-hits` | 否 | — | `1` | — |

### `autophone screens telemetry`

summarize operation screen_checkpoint telemetry

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--build` | 否 | — | `None` | — |
| `--out-root` | 否 | — | — | — |
| `--stale-threshold` | 否 | — | `0.5` | — |
| `--min-samples` | 否 | — | `3` | — |

### `autophone screens validate`

validate one screens/<build>.yaml asset

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<path>` | 是 | — | — | — |

### `autophone self-update`

managed release install, upgrade and rollback

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--install-root` | 否 | — | `<HOME>/autophone` | — |
| `--keep-releases` | 否 | — | `3` | — |

### `autophone self-update apply`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<source>` | 否 | — | — | — |
| `--version` | 否 | — | `None` | — |
| `--allow-irreversible` | 否 | — | `False` | — |
| `--full` | 否 | — | `False` | — |
| `--adopt` | 否 | — | `None` | import B-class state from a legacy flat install |

### `autophone self-update check`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<source>` | 是 | — | — | — |
| `--version` | 否 | — | `None` | — |

### `autophone self-update recover`

### `autophone self-update rollback`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--to` | 否 | — | `None` | — |
| `--restore-snapshot` | 否 | — | `False` | — |
| `--full` | 否 | — | `False` | — |

### `autophone self-update stage`

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `<source>` | 是 | — | — | — |
| `--version` | 否 | — | `None` | — |

### `autophone self-update status`

### `autophone serve`

start the web dashboard (FastAPI on /)

| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--host` | 否 | — | `'127.0.0.1'` | — |
| `--port` | 否 | — | `8000` | — |
| `--reload` | 否 | — | `False` | auto-reload on code changes (dev) |
| `--no-control-plane` | 否 | — | `False` | 强制走单 VM 兼容路径：忽略 AUTOPHONE_DATABASE_URL 与 start_vphone.sh 写的 control-plane.env（无 PostgreSQL 时的一次性排障用） |
| `--workers` | 否 | — | `1` | uvicorn 进程数；只支持 1（浏览器录制/远程操作的会话门是进程内锁） |
