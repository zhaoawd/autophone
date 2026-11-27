# 环境与验证矩阵

**简体中文** | [English](environment-matrix.en.md)

本页仅记录**本仓库当前已实际验证**的宿主环境、Python 与依赖组、`vphone-cli` 协议、iOS 客机镜像、部署模式以及 Accessibility（AX）后端配置。未列入的操作系统或 iOS 版本默认视为**尚未实机验证**。

## 1. 宿主操作系统与硬件架构

| 宿主环境 | 支持范围 | 验证依据 |
| --- | --- | --- |
| **macOS（Apple Silicon / `arm64`）** | 完整运行 `vphone-cli` iOS 虚拟机（`Virtualization.framework`）、原生 Swift Vision OCR、编译可选 iOS Guest 辅助探针（需 Xcode `iphoneos` SDK 与 `codesign`）以及全量离线测试 | 源码与 wheel 原生 Vision OCR 经 CI `macos-latest` 验证（`.github/workflows/ci.yml`）；真机回归与长跑在 Apple Silicon 宿主完成 |
| **Linux（`ubuntu-latest` CI）** | 纯 Python 逻辑、静态检查（`ruff`）、参考文档防漂移、公开内容检查、PostgreSQL Control Plane 平台测试、wheel/sdist 构建与独立安装校验、Playwright 模拟 API 看板 E2E 与 Frida Agent 单测 | `.github/workflows/ci.yml` 全部非 VM 任务运行于 `ubuntu-latest`；**不支持**运行 `vphone-cli` VM 与 macOS Vision OCR |

## 2. Python 版本、依赖组（`uv`）与附加工具链

- **Python 版本**：官方支持并经 CI 验证 **`3.11`、`3.12`、`3.13`**（`requires-python = ">=3.11,<3.14"`）。CI `build-and-install` 矩阵在 `3.11` / `3.12` / `3.13` 上同时校验 5 个 wheel 与 5 个 sdist 的干净虚拟环境安装；主测试与 lint 流水线运行于 `3.13`。不支持 `<3.11` 或 `3.14+`。
- **包管理与锁定**：使用 [`uv`](https://docs.astral.sh/uv/) 与仓库锁文件 `uv.lock`（核心包 `Pillow>=13.0.0`，锁文件固定 `13.0.0`）：

| `uv` 依赖组 / Extra | 包含组件 | 用途与说明 |
| --- | --- | --- |
| 基础核心（`autophone` + 4 子包） | `autophone-kernel`、`autophone-rig`、`autophone-domain`、`autophone-console` | CLI、Typed-IR 校验、Schema 契约、UDS 传输与核心执行框架（**零 `psycopg` 强依赖**，可直接运行离线校验、Web 控制台与单 VM 本地模式） |
| `--extra postgres` / `[postgres]` | `psycopg[binary]>=3.2` | **可选 PostgreSQL 驱动**；仅在启用多 VM Control Plane（`AUTOPHONE_DATABASE_URL`）或 `rig up --ephemeral-db` 时安装；未安装时触达 PostgreSQL 路径严格 fail-closed 并输出安装提示 |
| `--group platform` / `[platform]` | `fastapi`、`uvicorn`、`websockets`、`httpx2`、`croniter`、`psycopg[binary]` | 源码树完整平台开发与全量测试组（含 `scripts/test_public_platform.sh` 所需驱动）；独立 wheel/sdist 核心分发将 `psycopg` 拆出至可选 `[postgres]` extra |
| `--group contract` / `[contract]` | `pytest`、`pytest-xdist` | 离线单元测试与 `scripts/test_public_platform.sh` 平台套件 |
| `--group frida` / `[frida]` | 精确锁定 `frida==17.16.1`、`frida-tools==14.10.4`、`ddddocr>=1.6.1` | 仅在显式选用 `frida_inproc` AX、Frida `api_capture`、`crash_trap` 或灰盒探针时需要；**纯 OCR 与默认零 Frida `guest_axruntime` UI 回归不需要安装** |
| `--group dev`（仅源码开发） | `ruff>=0.16.6` | 源码静态检查（`uv run --only-group dev ruff check .`）；不进入 wheel/sdist 运行依赖 |

- **可选前端/Agent 测试工具链**：**Node.js `22+`**（CI 固定 Node `22` + Playwright Chromium）用于 `npm run test:dashboard-e2e` 与 `npm --prefix autophone/observation/frida/agent test`。

## 3. `vphone-cli` 协议与已验证的 iOS 客机镜像

| 维度 | 配置 / 版本 | 状态 | 说明与验证依据 |
| --- | --- | --- | --- |
| **`vphone-cli` 协议（推荐）** | `upstream_v1`（上游 [`Lakr233/vphone-cli`](https://github.com/Lakr233/vphone-cli) schema v2 VM 布局） | 公开核心推荐路径 | 通过 `autophone rig init <rig-id> --bundle /path/to/VPhone.bundle` 生成 `DeviceTarget`（要求绝对路径 `core_bundle`），经 UDS/vsock（`vphone.sock`）执行控制、系统定位与 UDS `apps.install`；`type` 写入 guest 剪贴板（`timeout >= 3s`）；无 `procexp` 时配合 `scripts/build_guest_helpers.sh` |
| **`vphone-cli` 协议（兼容）** | `legacy`（历史 checkout 布局，如 `vm-2607` 验证机） | 兼容保留 | `protocol: "legacy"`，旧版 SSH 装包/探针要求显式设置非空 `AUTOPHONE_SSH_PASSWORD`；Stage 2（9/9）与 Stage 3 / Phase 2B（20/20）真机验证在 `vm-2607` 完成 |
| **已验证 Guest iOS 版本（回归与 AX）** | **iOS `26.1`（构建号 `23B85`）** | 已完成真机闭环验证 | 在 `vm-2607` 上覆盖 `Food Ordering`（React Native）、系统 `Settings`（`com.apple.Preferences`，SwiftUI）与 `SpringBoard` 控制中心（Round 1、Round 1.5 `ax_dump-v9` 5/5、Stage 2 联合裁决 9/9、Stage 3 `guest_axruntime` 零 Frida 20/20 长跑，详见[实测验证报告](../concepts/app-symbols-v1-validation-report.md)） |
| **已验证 Guest iOS 版本（`upstream_v1` + `vphone_mcp` + `guest_axruntime`）** | **iOS `27.0.1`（构建号 `24A446`）** | 已完成 `upstream_v1` 10/10 与 `guest_axruntime` 全链路实机验收 | 在 `upstream_v1` `vphone-cli` 虚拟机上完成：(1) `vphone_mcp` 10/10 实机验证（`wait_ready`、`get_capabilities`、JPEG `screenshot` 几何、`list_apps`、系统定位往返、`local_lock` 生命周期/争用/隔离以及 Control Plane 不可达拒止与真实租约往返）；(2) 默认零 Frida `guest_axruntime` 全链路实测（`guest_ax_probe` 自动推送与缓存复用、`Settings` `12/14` 属性 `5019` `axIdentifier` 与 `10×` 直采访客 P50 `102.11 ms` / `AxSession.refresh()` P50 `369.80 ms`、实时 SpringBoard `"Trust This Computer?"` 系统弹窗/Home/控制中心/锁屏 4/4 fail-closed 拦截、`ios-settings-smoke` `R1`/`R2`/`R3_AX` ×2 与 `Food Ordering` 零 Frida `tap_ax` 导航、`SpringBoard` `autophone-springboard-regions` 巡检 `0 / 1,027` `Tag: 255` 且 `voiceover=false`，详见[实测验证报告 §七](../concepts/app-symbols-v1-validation-report.md)） |
| **其他 iOS 版本** | 未列入上述已验证构建的 iOS 版本 | 尚未实机验证 | 可选 Guest 探针编译脚本声明 `-mios-version-min=16.0`（`arm64 + arm64e`），但仅上述两版 iOS 构建具备仓库内真机实证；接入其他镜像前请按快速开始与体检流程逐项验证 |

## 4. 部署模式矩阵

| 部署模式 | 启动入口 | 必需组件 | 隔离与 Fail-Closed 行为 |
| --- | --- | --- | --- |
| **零 VM 离线模式** | `autophone serve`（`/embed/device-console?demo=1`）、`autophone app case lint`、`scripts/verify_core_install.py` | Python `3.11`–`3.13` + 基础核心（或源码树 `--group platform`） | 无需 VM、无需 PostgreSQL / `psycopg`、无需 Frida |
| **单 VM 本地模式** | `autophone rig up --target <target.json>`（单个 `--target` 且未设 `AUTOPHONE_DATABASE_URL`） | Apple Silicon macOS + 单台 `vphone` VM + Xcode CLT | 无需 PostgreSQL 服务，独立 wheel/sdist 安装亦无需安装 `psycopg`；单机 `ScreenLock` 串行保护 |
| **多 VM Control Plane 模式** | `autophone rig up --target rig-a.json --target rig-b.json`（配合 `AUTOPHONE_DATABASE_URL` 或 `--ephemeral-db`） | **PostgreSQL `16`** + 可选 **`autophone[postgres]`**（CI 与 `scripts/test_public_platform.sh` 实测）+ 多台隔离 `DeviceTarget` | 每台 Rig 独立 `autophone-rig-agent` 与 `ScreenLock`；缺失 `psycopg` 或控制面不可达时严格 fail-closed（CLI 退出码 `2`、HTTP `503`），绝不静默直连 UDS |
| **[`vphone_mcp/`](../../vphone_mcp/README.md) MCP 服务** | `uv --directory vphone_mcp run vphone-mcp`（支持 `AUTOPHONE_VPHONE_PROTOCOL=upstream_v1\|legacy`） | 独立包 `vphone_mcp/` | 支持 `control_plane` / `local_lock` / `managed_proxy` `ScreenLock` 协同（配置控制面不可达时 fail-closed，独立模式可选 `VPHONE_MCP_REQUIRE_LOCK=1`）与 `upstream_v1` 线协议；已在 `upstream_v1`（iOS `27.0.1 / 24A446`）通过 10/10 实机闭环验收，正式转正 |

## 5. Accessibility（AX）后端矩阵

单次 Run 的 `ax_backend` 在启动期冻结且严格隔离证据（`claims.json`、`ax_comparison`），运行期间不做静默回退：

| AX 后端（`capabilities.ax.backend`） | 当前角色 | Guest 辅助产物与环境变量 | Frida 依赖 | 已验证特性与边界 |
| --- | --- | --- | --- | --- |
| **`guest_axruntime`** | **默认 AX 后端**（`DEFAULT_AX_BACKEND`，纯 UI 回归与反注入场景开箱零 Frida） | `support/guest_ax_probe`（`scripts/build_guest_ax_probe.sh` / `AUTOPHONE_GUEST_AX_PROBE`，单条最小权限 `com.apple.accessibility.api = true`） | **零 Frida 依赖**（亦可与 Frida `api_capture` 9/9 共存无冲突） | iOS `26.1`（`23B85`，`legacy`）20/20 多应用零 Frida 长跑通过（46/46 探针 0 失败、端到端 P50 `267.14 ms`、`0` 个 `Tag: 255` 脏页、`0` 次 VoiceOver 激活）；iOS `27.0.1`（`24A446`，`upstream_v1`）全链路零 Frida 实测通过（`Settings` 访客 P50 `102.11 ms` / `refresh()` P50 `369.80 ms`、系统弹窗/Home/控制中心/锁屏 4/4 fail-closed、`ios-settings-smoke` + `Food Ordering` `tap_ax` 通过、`SpringBoard` `0 / 1,027` `Tag: 255`、`voiceover=false`）；不产生进程内灰盒字段 |
| **`frida_inproc`** | **显式可选（`capabilities.ax.backend: frida_inproc`）与进程内灰盒诊断通道** | `support/ax_dump.dylib`（`dumper_version: 9`，`scripts/build_ax_dump.sh` / `AUTOPHONE_AX_DYLIB`） | 需要 `--group frida`（`frida==17.16.1`）与 guest `frida-server`（`27042`） | 支持进程内 `variant_observation`（如 `core_location_authorization`）、`modal_sibling` 诊断与 `api_capture`；卸载后在目标 App 留存 `Tag: 255` 页（由 Run 后重启清理，严禁附加 `SpringBoard`） |

> 独立 wheel / sdist 安装环境下编译与配置三类可选 Guest 探针（SpringBoard 区域探针、`guest_ax_probe` 与 `ax_dump.dylib`）的完整步骤，见[核心分发安装与验证](../public-core.md#可选-guest-ax-探针wheel--sdist-安装)。

## 6. 常用离线验证命令

```bash
# 1. 静态语法与 lint 检查
uv run --only-group dev ruff check .

# 2. 自动生成参考文档防漂移 + 公开内容与链接检查
uv run --group platform python scripts/generate_public_docs.py --check && python3 scripts/check_public_content.py

# 3. 公开平台离线测试套件（自动拉起临时 Unix-socket PostgreSQL 或 Docker postgres:16）
scripts/test_public_platform.sh

# 4. 看板浏览器 E2E（Playwright 模拟 API）
npm run test:dashboard-e2e

# 5. 宿主原生 Vision OCR 探针（仅 macOS）
uv run python -m autophone.device.ocr_target probe
```

另见[快速开始](quickstart.md)、[常见问答（FAQ）](faq.md)与[核心分发安装与验证](../public-core.md)。
