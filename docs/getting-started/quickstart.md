# 快速开始

**简体中文** | [English](quickstart.en.md)

本指南提供两条阶梯路径：**零 VM 离线体验**（无需启动任何虚拟机）与 **单台 `vphone` 运行真实回归**（无需准备第三方 IPA 或外部 PostgreSQL）。

## 环境要求

- macOS（运行 `vphone-cli` 与 Vision OCR）；宿主纯逻辑与文档检查亦可在 Linux CI 运行
- Python `3.11`–`3.13` 与 [`uv`](https://docs.astral.sh/uv/)
- 浏览器回归可选 Node.js `22+`

## Step 0：零 VM 离线体验

从源码根目录安装核心运行与测试依赖：

```bash
uv sync --locked --group platform --group contract
```

### 1. 体验离线设备控制台交互页

启动本地看板并在浏览器打开内置演示页 `/embed/device-console?demo=1`：

```bash
export AUTOPHONE_STATE_DIR="$PWD/.tmp-state"
uv run --group platform autophone serve --host 127.0.0.1 --port 8000
```

在浏览器访问 `http://127.0.0.1:8000/embed/device-console?demo=1`，无需连接真实 VM 即可预览远程点击、剪贴板粘贴与会话状态交互。

### 2. 运行离线 Recipe 校验与合成 OCR 正反例

无需真实设备即可验证用例库静态校验与合成 OCR 断言执行：

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/ios-settings-smoke"
uv run --group platform autophone app show ios_settings_smoke
uv run --group platform autophone app case lint ios_settings_smoke
uv run --group platform python scripts/verify_core_install.py examples/ios-settings-smoke
```

## Step 1：单台 `vphone` 运行免 IPA 真机回归

宿主需要 macOS 和 Xcode Command Line Tools。首次 OCR 会将随包 Swift 源码编译至用户缓存；可用 `AUTOPHONE_OCR_BIN` 指定已有可执行文件。示例使用英文 Settings 主列表。运行 `uv run python -m autophone.device.ocr_target probe` 可先用固定图片检查原生识别。

按照 [vphone-cli 上游文档](https://lakr233.github.io/vphone-cli/) 准备好 `VPhone.bundle` 与一台 schema v2 的 VM（以 `rig-a` 为例，默认位于 `~/.vphone/machines/rig-a`）。

### 1. 一条命令生成 DeviceTarget 配置

```bash
uv run --group platform autophone rig init rig-a \
  --bundle /absolute/path/to/VPhone.bundle
```

命令会自动推导 `vm_dir`、`vphone.sock`、运行目录、从 `udid-prediction.txt` 读取 `UDID` 并分配不冲突的宿主端口，写入 `~/.vphone/machines/rig-a/autophone/target.json`。

### 2. 一条命令拉起单台 VM 与 Web 控制台（免预装 PostgreSQL）

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/ios-settings-smoke"
uv run --group platform autophone rig up \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json"
```

传入单个 `--target` 且未设置 `AUTOPHONE_DATABASE_URL` 时，`autophone rig up` 会自动以单机本地模式启动该 VM 与看板，并在就绪后输出 `Dashboard: http://127.0.0.1:8000`。

### 3. 运行免 IPA 示例并查看 HTML 证据报告

打开 `http://127.0.0.1:8000`，展开“**用例回归、IPA 与录制**”：

1. 选择 `iOS Settings Smoke`（基于系统自带 `com.apple.Preferences`，无需安装第三方 IPA 或编译 SpringBoard 探针）。
2. 点击正向用例 `R1` 的“**在所选 Rig 运行**”观察 `passed` 结论，再点击负向对照用例 `R2` 观察 `failed` 证据。（提示：若 VM 空闲息屏黑屏导致首次运行提示 `app_ui_unavailable`，可先在设备控制台点击两次 `Home` 唤醒解锁至主屏后再运行。）
3. 点击运行列表中的“**查看 HTML 报告**”打开包含截图与逐步断言结果的报告页。

也可在另一终端通过 CLI 运行同样用例：

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/ios-settings-smoke"
uv run --group platform autophone --app ios_settings_smoke --device rig-a run --ids R1 R2
```

在本机单 Rig 看板上，可重复执行 `python3 scripts/verify_settings_run.py --port 8000 --rig rig-a`。该命令会向指定测试设备提交 R1、R2，检查断言结果并回读两份 HTML 报告。

## 下一步

- 已验证的宿主 / Python / iOS / AX 配置清单：[环境与验证矩阵](environment-matrix.md)
- 环境选型与能力边界：[FAQ](faq.md)
- 编写自定义 App 用例与 Recipe：[编写用例与 Recipe](../guides/authoring-cases.md)
- 第三方插件与 Entry Point 扩展：[公开 Entry Point 扩展指南](../guides/extensions.md)
- 查询全部可用动作词表：[Verb 参考手册](../reference/verbs.md)
- 多台 VM 与 PostgreSQL 控制面：[多 VM 控制面部署](../guides/multi-vm-control-plane.md)
- 安装 wheel 分发与可选 Guest 探针：[核心分发安装与验证](../public-core.md)
