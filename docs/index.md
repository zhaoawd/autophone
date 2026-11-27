# Autophone 文档中心

**面向 [`vphone-cli`](https://github.com/Lakr233/vphone-cli) 的确定性 iOS 回归测试与多 VM 设备控制面。**

Autophone 在 Apple Silicon Mac 上将越狱 iOS 虚拟机封装为可追溯、可隔离、可复现的工程测试平台：按精确 **Build ID** 追踪设备版 IPA、以静态校验的 **Typed-IR Recipe** 与 **Vision OCR / Accessibility (AX)** 语义寻址执行回归（正式运行期不依赖 AI 临场决策）、通过 **ScreenLock** 串行保护多 Rig 屏幕与系统定位会话，并沉淀自包含 HTML 证据报告。

## 快速导航

- **[零 VM 与单机上手（Quickstart）](getting-started/quickstart.md)** · **[English Quickstart](getting-started/quickstart.en.md)** — 零虚拟机离线体验与单台 `vphone` 免 IPA 真机回归路径
- **[环境与验证矩阵（Environment Matrix）](getting-started/environment-matrix.md)** · **[English Matrix](getting-started/environment-matrix.en.md)** — 仓库实测覆盖的 macOS / Python / `vphone-cli` / iOS 26.1 / Control Plane 与 AX 后端矩阵
- **[常见问答（FAQ）](getting-started/faq.md)** · **[English FAQ](getting-started/faq.en.md)** — 与 Xcode Simulator、实体 iPhone 的能力边界对比及常见问题
- **[交互式架构图（Architecture Diagram）](architecture.html)** — 浏览公开核心组件边界与运行期数据流
- **[核心分发安装与验证](public-core.md)** — 五个 wheel / sdist 独立构建、安装验真与可选 Guest 探针（SpringBoard / `guest_ax_probe` / `ax_dump.dylib`）编译配置

## 使用指南（Guides）

- [设备控制台与嵌入](guides/device-console.md) · [Device Console (EN)](guides/device-console.en.md) — Web 控制台、离线演示页 `demo=1` 与一次性启动凭证嵌入
- [系统定位与路线回放](guides/location.md) · [System Location (EN)](guides/location.en.md) — 按 Rig 控制定点、内置路线回放与坐标回读
- [IPA 构建库与录制回放](guides/packages-and-recording.md) · [Packages & Recording (EN)](guides/packages-and-recording.en.md) — Build ID 追踪、安装前检查与录制草稿回放
- [编写用例与 Recipe](guides/authoring-cases.md) · [Authoring Cases (EN)](guides/authoring-cases.en.md) — App Profile、Typed-IR Recipe 与用例准入流程
- [多 VM 控制面部署](guides/multi-vm-control-plane.md) · [Multi-VM Control Plane (EN)](guides/multi-vm-control-plane.en.md) — PostgreSQL Control Plane、多 Rig 编排与 `ScreenLock` 隔离
- [公开 Entry Point 扩展指南](guides/extensions.md) · [English Extension Guide](guides/extensions.en.md) — `autophone.run_admission`、`autophone.executor`、`autophone.device_session` 协议契约与最小第三方模板

## 核心概念（Concepts）

- [术语与概念模型](concepts/terminology.md) — Rig、Control Plane、ScreenLock、Build Record 与 Verdict 等核心名词
- [回归运行期契约](concepts/runtime-contract.md) — 确定性回归运行期不变量与 AI 使用边界
- [App 受控操作语言全局蓝图](concepts/app-operation-language.md) — 围绕防错包、防错点、防弱断言的受控执行架构与务实演进边界
- [统一 App 符号头文件最小契约（`app-symbols.v1`）](concepts/app-symbols-v1.md) — 不可变制品绑定、独立关系证据、多副作用隔离准入与首轮对照实验规格
- [Guest 免侵入 AX 与双通道分治](concepts/guest-ax-observation.md) — 单次运行固定后端、进程外 `AXRuntime` 独立实验与实测迁移门禁
- [受控操作与符号绑定分阶段实施任务书](concepts/app-symbols-v1-plan.md) — 8 项锁定架构决策、Round 1（M1–M6）工程拆解与后续专项触发门禁
- [符号绑定与 Guest AX 实测验证报告](concepts/app-symbols-v1-validation-report.md) — Round 1 双版本与 L1 双分母、Round 1.5 `ax_dump-v9` 遮挡加固与 Stage 3 `guest_axruntime` 四项对比实测结论
- [定位能力域规范](domains/location.md) — 统一位置会话、坐标系转换边界与四级事实分离
- [ADR-0005 统一定位会话](adr/0005-unify-location-sessions-keep-target-runtimes-separate.md) — 系统目标与 App 目标独立运行时的架构决策记录

## 代码自动生成参考（Auto-Generated References）

以下参考文档直接从源码真源生成，并在 CI 中执行防漂移校验：

- [Recipe Verb 动作手册](reference/verbs.md)
- [CLI 命令参考](reference/cli.md)
- [核心 JSON Schema 字典](reference/schemas.md)
- [HTTP API 接口目录](reference/http-api.md)

## 社区与许可

Autophone 公开核心基于 [Apache License 2.0](https://github.com/zhaoawd/autophone/blob/main/LICENSE) 发布（版权与上游 `vphone-cli` 致谢见 [NOTICE](https://github.com/zhaoawd/autophone/blob/main/NOTICE)）。

- [GitHub 仓库](https://github.com/zhaoawd/autophone) · [README（English）](https://github.com/zhaoawd/autophone/blob/main/README.md) · [README（简体中文）](https://github.com/zhaoawd/autophone/blob/main/README.zh-CN.md)
- [参与开发（English）](https://github.com/zhaoawd/autophone/blob/main/CONTRIBUTING.md) · [参与开发（简体中文）](https://github.com/zhaoawd/autophone/blob/main/CONTRIBUTING.zh-CN.md)
- [安全策略（SECURITY.md）](https://github.com/zhaoawd/autophone/blob/main/SECURITY.md) · [行为准则（CODE_OF_CONDUCT.md）](https://github.com/zhaoawd/autophone/blob/main/CODE_OF_CONDUCT.md)
- [发布说明（RELEASE-NOTES.md）](https://github.com/zhaoawd/autophone/blob/main/RELEASE-NOTES.md) · [变更记录（CHANGELOG.md）](https://github.com/zhaoawd/autophone/blob/main/CHANGELOG.md)
