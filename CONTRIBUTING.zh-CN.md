# 参与开发（Contributing）

[English](CONTRIBUTING.md) | **简体中文**

欢迎向 [Autophone](https://github.com/zhaoawd/autophone) 提交问题报告、文档改进、测试补充与代码变更。本仓库是公开核心的唯一开发真源（Single Source of Truth）。

## 1. 开发环境准备

- Python `3.11`–`3.13` 与 [`uv`](https://docs.astral.sh/uv/)
- Node.js `22+`（用于看板 Playwright E2E 与 Frida agent 单元测试）
- 真机 VM 操作与宿主原生 Vision OCR 需 macOS + Xcode Command Line Tools（纯逻辑、PostgreSQL 控制面测试与发行包构建校验亦可在 Linux 运行）

```bash
uv sync --locked --group dev --group platform --group contract
# 或使用 Makefile 快捷命令：
make sync
```

## 2. 多包分层与文件归属

仓库采用单源码树（`autophone/`）按归属切分为 5 个发行包的构建模型：

| 包名 | 职责边界 |
| --- | --- |
| `autophone-kernel` | JSON Schemas、数据契约、产物清单与基础公共组件 |
| `autophone-rig` | `vphone-cli` UDS/生命周期驱动、Rig Agent、`ScreenLock` 与 PostgreSQL Control Plane |
| `autophone-domain` | Typed-IR Recipe 执行器、IPA Package Store、系统定位、OCR/AX 观测与准入校验 |
| `autophone-console` | `autophone` CLI 入口与 Web 设备控制台/看板 |
| `autophone` | 顶层聚合元数据包 |

新增源码文件时，请确认其路径命中 [pkgs/ownership.py](pkgs/ownership.py) 中的对应组件归属规则，并严守核心运行期契约（正式回归运行期禁止调用 `autophone.application.ai` 临场决策，AI 仅限离线编写、探索与事后诊断，产出资产一律以 `draft` 进入；Typed-IR Recipe 仅使用已注册 Verb 与 Operation）。

## 3. 提交前本地门禁

提交 Pull Request 前，可先运行零数据库快速本地门禁（包含 `ruff`、参考文档防漂移、公开内容/链接检查与 Frida agent 单测）：

```bash
make check
```

运行 `make help` 可查看全部快捷目标。也可以按需直接执行各项显式校验命令：

```bash
# 1. 静态检查、自动生成参考文档防漂移、公开内容与相对链接检查
uv run --only-group dev ruff check .
uv run --group platform python scripts/generate_public_docs.py --check
python3 scripts/check_public_content.py

# 2. Frida agent Node.js 单元测试（需 Node.js 22+）
npm --prefix autophone/observation/frida/agent test

# 3. 平台集成回归（自动支持本机 PostgreSQL initdb/pg_ctl、
#    Docker postgres:16 回退，或外部 AUTOPHONE_TEST_POSTGRES_DSN）
scripts/test_public_platform.sh

# 4. 看板浏览器 E2E（改动前端资源或 HTTP 路由时执行）
npm ci && npx playwright install chromium && npm run test:dashboard-e2e

# 5. 五个核心包 wheel / sdist 干净构建与归档/隔离校验
scripts/build_public_core.sh dist/core
```

若改动了 CLI 参数、Recipe Verb、JSON Schema 或 HTTP 路由，请先运行以下命令更新 `docs/reference/` 下的自动生成文档，并按[版本兼容与废弃策略](CHANGELOG.md#versioning--deprecation-policy)在 `CHANGELOG.md` 标注破坏性变更或废弃提示：

```bash
uv run --group platform python scripts/generate_public_docs.py --write
# 或：make docs-write
```

## 4. 问题反馈与贡献许可

- **提交 Bug**：请尽量附上提交 SHA 或版本号、操作系统、Python 版本、运行模式（单机本地 / Control Plane）、最小复现步骤及脱敏后的日志或断言摘要。
- **安全漏洞**：请勿提交公开 Issue，请参阅 [安全策略](SECURITY.md) 通过私密渠道报告。
- **行为准则**：参与社区协作请遵守 [行为准则](CODE_OF_CONDUCT.md)。
- **贡献许可（Inbound = Outbound）**：根据 [Apache-2.0 许可证](LICENSE) 第 5 条（Submission of Contributions），除非另有明确书面声明，你向本仓库提交的任何贡献默认按同一 Apache-2.0 许可证授权发布。
