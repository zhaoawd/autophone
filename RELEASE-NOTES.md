# Autophone 核心平台发布说明（v0.2.0）

> 本文面向部署者与用户提供 `v0.2.0` 的详细发布说明；各版本按时间排列的精简变更摘要请参阅 [CHANGELOG.md](CHANGELOG.md)。

`v0.2.0` 围绕**零 Frida 默认可访问性观测与执行**、**不可变应用符号契约与多证据联合判定**、**`vphone_mcp` 正式毕业**、**内核分层解耦与可选 PostgreSQL 驱动**以及**公开扩展插件体系与双语文档**完成全面升级。

## 公开源码与分发范围

公开源码涵盖五个核心发行包（`autophone`、`autophone-kernel`、`autophone-rig`、`autophone-domain`、`autophone-console`）及独立 MCP 服务包（`vphone_mcp/`），附带数据库迁移、复验入口、双语入门与参考文档、可选示例（`examples/ios-settings-smoke`、`examples/food-ordering` 与 `examples/plugin-template`），以及可选只读 guest 辅助探针源码（`support/springboard-regions/`、`support/guest-ax-probe/`、`support/ax-dump/`）。

- **内核解耦（`autophone-kernel`）**：彻底移除 `autophone/assets/{app_symbols,symbol_tools,entity_binding}.py` 对 `autophone-domain` 的上层依赖，通过纯 kernel 模块（`_package_manifest.py`、`api_assertions.py`、`effect_markers.py`）与 domain 钩子注册完成分层隔离；`tests/test_kernel_layering.py` 与 `scripts/verify_core_archives.py` 对全部 47 个 kernel 模块强制 `0` 违规并验证 `autophone-kernel` 独立 wheel 导入。
- **可选 PostgreSQL 驱动（`autophone[postgres]`）**：将 `psycopg[binary]` 从 `autophone-rig` 基础依赖迁出至可选 `[postgres]` extra，并在 `autophone/control/_psycopg.py` 实现懒加载 fail-closed。单 VM 本地模式、离线校验与基础 wheel/sdist 安装不再依赖 PostgreSQL 驱动；启用多 VM Control Plane（`AUTOPHONE_DATABASE_URL` 或 `--ephemeral-db`）时按需安装 `autophone[postgres]`。`scripts/build_public_core.sh` 现同步打包完整的 `examples/food-ordering` 资产树。

## 零 Frida 默认 AX 后端与 App Operation 演进（`app-symbols.v1`）

### 1. 不可变应用符号契约与多副作用治理（`app-symbols.v1`）

- 新增 `app-symbols.v1.json` 契约、纯内容摘要路由（`symbols/artifacts/<digest>.yaml`）与外置声明文件 `symbols/claims.json`（记录 `symbol.unique_visibility` 实测证据而不修改 `binding_sha256`）。
- 统一加载期 `tap_ax(symbol=...)` 展开，提供 L2 构建清单导入与 L1 React Native `main.jsbundle` 提取命令（`autophone app symbols import-l2` / `extract-l1-rn`）及双分母覆盖率报告。
- 副作用治理升级为多动词集合与显式 `completeness`（`exhaustive` / `lower_bound` / `unknown`）：拒绝多副作用符号动作使用 `"irreversible"` 通配、硬阻断未完备 `active` 资产，并要求未完备 `draft` 试跑提供显式 CLI 确认与 `trial_data_isolation: "isolated"`。

### 2. Round 1.5 AX 遮挡加固（`ax_dump-v9`）与严格目标校验

- 新增 `support/ax-dump/ax_dump.m`（协议下限 `dumper_version: 9`）与 `scripts/build_ax_dump.sh`，支持 `accessibilityViewIsModal` 同层模态遮挡检测、前台激活检查、`screens.yaml` 的 `blocking_overlays` 点击前阻断，以及符号绑定用例的全局自动遮罩消除抑制。
- 扩展 `tap_ax` 的单帧 `screen_id` 预检与 `require_unique` 零派发 fail-closed（`ambiguous_match` / `locator_missing` / `screen_mismatch`）。
- 更新 `operation-ax-checkpoint.v2.json` 以支持 `build-identity.v2.json` 与 `ax_backend`；在 `build_identity.py` / `run_execution.py` 中增加 Package Store `build_marker` 字节级校验以安全写入 `symbol.unique_visibility` 声明，并在 `promotion.py` 中将无摘要旧探针、同版本号重打包 IPA 或 `binding_sha256` 漂移统一标记为 `discrimination_stale`。

### 3. Stage 2 实体绑定、动作后回读与联合判定（Joint Verdict）

- 新增 `entity-binding.v1.json`、用例级实体绑定（`autophone/assets/entity_binding.py`）、动作后受控 Fixture/API 回读（`autophone/execution/entity_readback.py`）、API 窗口请求/响应实体断言，以及 UI × API × 回读 fail-closed 联合判定（`autophone/observation/joint_verdict.py`）。
- 已通过离线套件与真机 iOS 26.1 VM（9/9 矩阵）实测验证。

### 4. Stage 3 进程外 `guest_axruntime` 默认零 Frida 后端

- 新增独立零注入 `AXRuntime` 探针（`support/guest-ax-probe/`、`scripts/build_guest_ax_probe.sh`，仅声明单条 `com.apple.accessibility.api` entitlement），集成至 `AxSession`、`tap_ax`、`ax_checkpoint` v2 与 `variant_evidence`（`ui_structure` 与 `inproc_graybox` 静态隔离），内置前台 / 控制中心 / 锁屏门禁与零 VoiceOver 强制检查（`_AXSVoiceOverTouchEnabled == 0`）。
- 在完成 20/20 多应用零 Frida 稳定性压测并补齐运行间主屏重置前置（`AppProfile.reset`、`RigMaintenance._reset_home_and_probe()` 的 `reset: relaunch` 回退以及 `examples/ios-settings-smoke` 根屏 `ready_markers`）后，已将默认 AX 后端由 `frida_inproc` 正式切换为 `guest_axruntime`（`DEFAULT_AX_BACKEND`）。
- 已补充完成 `upstream_v1` + iOS `27.0.1`（`24A446`）真机实测验证（`guest_ax_probe` UDS 自动部署与缓存复用、USB 信任弹窗 / 主屏 / 控制中心 / 锁屏 4/4 跨进程 fail-closed 遮挡门禁、默认零 Frida `ios-settings-smoke` 与 `Food Ordering` 回归、`voiceover=false` 与 `SpringBoard` `Tag: 255 == 0`），并修复 `autophone/device/guest_ax_probe.py` 在 iOS `27.0.1` 锁屏/息屏缩放帧（`foreground_active=False`）下的几何解析，确保保留 `foreground_occlusion_reason="screen_locked"` 且前台活跃期仍严格 fail-closed。
- 未显式指定 `capabilities.ax.backend` 的 App Profile 现默认以纯零 Frida 模式执行：未启用 API 捕获时跳过 Frida 就绪检查与 attach、`crash_trap` 默认关闭、`app_frida_hygiene` 记录 `no_frida_attached` 且避免不必要的应用终止，并按需为 `requires={"ax"}` / `tap_ax` 操作提供懒加载 `AxSession`。
- `frida_inproc` 仍作为显式可选的进程内灰盒后端保留（通过 `capabilities.ax.backend: frida_inproc` 或 `borrowed_lifetime` 启用，用于 `api_capture`、`core_location_authorization`、`modal_sibling` 等能力），跨后端声明与对比严格隔离，未知后端启动期 fail-closed。
- wheel/sdist 独立安装环境中如需启用 AX 能力，可在 macOS 宿主按 [docs/public-core.md](docs/public-core.md) 本地编译并通过 `AUTOPHONE_GUEST_AX_PROBE` 或 `AUTOPHONE_AX_DYLIB` 注入产物路径。

## `vphone_mcp` 正式毕业（ScreenLock / Control Plane 协同与 `upstream_v1`）

`vphone_mcp/` 已从实验入口正式毕业为受支持的 MCP 设备控制面组件：

- 支持 `managed_proxy`、`control_plane`、`local_lock` 与 `standalone`（`VPHONE_MCP_REQUIRE_LOCK=1`）四种并发协同模式，与 Autophone 核心 `ScreenLock` 互通；当配置了 Control Plane 但不可达时严格 fail-closed，不降级直连设备 socket。
- 支持 `upstream_v1` 与 `legacy` UDS 线协议帧编解码，区分只读与变更类 MCP 工具并在变更操作上强制锁校验。
- 随仓附带完整单元测试与根仓库互操作套件（`vphone_mcp/tests/`、`tests/test_vphone_mcp_coordination.py`；已消除 `test_upstream_v1.py` 半关闭探针时序竞态并通过 19/19 稳定复跑），并已在运行中的 `upstream_v1` `vphone-cli` VM（iOS `27.0.1 / 24A446`）上通过 10/10 实测验证。

## 公开 Entry Point 扩展体系与双语文档

- **Public Entry Point 扩展**：新增中英双语扩展开发指南（[docs/guides/extensions.md](docs/guides/extensions.md) / [docs/guides/extensions.en.md](docs/guides/extensions.en.md)）、可运行第三方最小插件模板（`examples/plugin-template/`）与协议测试（`tests/test_extension_entrypoints.py`），覆盖 `autophone.run_admission`、`autophone.executor` 与 `autophone.device_session` 三个公开扩展点。
- **双语入门、功能指南与环境矩阵**：新增英文快速开始与 FAQ（[docs/getting-started/quickstart.en.md](docs/getting-started/quickstart.en.md)、[docs/getting-started/faq.en.md](docs/getting-started/faq.en.md)）、5 篇核心功能指南的完整英文版（`docs/guides/{authoring-cases,device-console,location,multi-vm-control-plane,packages-and-recording}.en.md`）与双语切换入口，以及中英双语环境与验证矩阵（[docs/getting-started/environment-matrix.md](docs/getting-started/environment-matrix.md)、[docs/getting-started/environment-matrix.en.md](docs/getting-started/environment-matrix.en.md)），清晰区分各协议与 iOS 版本的实测边界。

## 安装前检查、依赖安全与凭证处理

- **安装前只读检查**：第三方 IPA 传输前默认检查 SpringBoard 内存区域标签，检出 Tag 255 残留或读取失败时按 fail-closed 拒绝安装；无 `procexp` 的上游 guest 可通过 `scripts/build_guest_helpers.sh` 编译并签名 `support/springboard-regions/springboard_regions.c`，详见 [docs/public-core.md](docs/public-core.md#安装前检查)。
- **运行环境与安全基线**：支持 Python `3.11`–`3.13`，核心包要求 `Pillow >= 13.0.0`（锁文件固定 `13.0.0`）；看板访问日志省略查询参数，嵌入式控制台禁止缓存与 Referrer 泄露并在兑换后立即清除 URL 中的一次性启动令牌；旧版 SSH 传输要求显式配置非空 `AUTOPHONE_SSH_PASSWORD`。漏洞报告与响应 SLA 见 [SECURITY.md](SECURITY.md)。

## 复验入口

- **平台与契约离线套件**：运行 `scripts/test_public_platform.sh` 验证临时 PostgreSQL、设备会话通道、系统定位、IPA 安装契约、多 Rig 目标隔离与 MCP 协同。
- **内核分层、可选驱动与打包/扩展测试**：运行 `uv run --group contract --group platform pytest tests/test_kernel_layering.py tests/test_optional_postgres.py tests/test_extension_entrypoints.py tests/test_vphone_mcp_coordination.py tests/test_public_packaging_hygiene.py`。
- **`vphone_mcp` 独立测试**：运行 `uv --directory vphone_mcp run python -m unittest discover -s tests -v`。
- **看板 Playwright E2E**：运行 `npm run test:dashboard-e2e` 验证浏览器切换、定向请求、断线显示与离线演示。
- **静态检查、文档漂移与公开内容门禁**：运行 `uv run --only-group dev ruff check .` 以及 `uv run --group platform python scripts/generate_public_docs.py --check && python3 scripts/check_public_content.py`。
- **独立 wheel / sdist 构建与校验**：运行 `scripts/build_public_core.sh dist/core`，并按 [docs/public-core.md](docs/public-core.md) 使用 `scripts/verify_core_install.py` 核对五个 wheel 与五个 sdist 的独立安装与合成 OCR 校验。实际设备验收边界见 [README.md](README.md) 与 [环境与验证矩阵](docs/getting-started/environment-matrix.md)。
