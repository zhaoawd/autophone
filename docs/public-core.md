# 核心分发安装与验证

在源码根使用 `scripts/build_public_core.sh dist/core` 生成五个 wheel、五个 sdist 和可选示例。构建并安装时需要 Python 3.11–3.13 及第三方依赖缓存或索引。

## 独立安装

从源码根执行以下命令。在验证 sdist 时，新建一个虚拟环境，将安装命令中的 `*.whl` 换为 `*.tar.gz`。

```bash
python -m venv .venv-core
. .venv-core/bin/activate
python -m pip install dist/core/*.whl
python -m pip check
python -I scripts/verify_core_install.py examples/food-ordering
autophone --help
```

独立验证覆盖 CLI、HTTP 看板、无 App Profile 启动、可选示例加载及合成 OCR 正反例。基础核心 wheel/sdist 安装**不包含 `psycopg`**，即可直接满足离线校验、Web 控制台与单 VM 本地模式；仅在启用多 VM PostgreSQL 控制面（`AUTOPHONE_DATABASE_URL` 或 `--ephemeral-db`）时按需追加 `pip install "autophone[postgres]"`（或源码环境 `uv sync --extra postgres`），未安装驱动时触达 PostgreSQL 路径会严格 fail-closed 并提示安装命令。[发布说明](https://github.com/zhaoawd/autophone/blob/main/RELEASE-NOTES.md)列出自动化复验入口。真实设备结果仅适用于验收回执中记录的源码提交和发行包。准备设备和启动看板见[快速开始](getting-started/quickstart.md)与 [README](https://github.com/zhaoawd/autophone/blob/main/README.md)。

## 运行时配置

旧版 SSH 探针、启动脚本和 SSH 文件传输从各自进程的 `AUTOPHONE_SSH_PASSWORD` 读取 guest 口令。使用旧版 SSH 功能时为对应 Rig 注入非空值。

执行和覆盖率工具要求显式提供用例库路径，录制工具要求显式选择 App，独立 App 观测探针要求显式提供 Bundle ID。Food Ordering 的配置示例见[可选示例](https://github.com/zhaoawd/autophone/blob/main/FOOD-ORDERING.md)。核心分发的设备管理范围包括截图、控制、IPA 管理和系统定位。

核心 CLI 提供 `rig up` 和按 Rig 定向的 `location`，使用示例见[快速开始](getting-started/quickstart.md)、[系统定位与路线回放](guides/location.md)与 [README](https://github.com/zhaoawd/autophone/blob/main/README.md)。

## 安装前检查

安装前核对 SpringBoard 的 PID、启动时间和内存区域标签。检出 Tag 255，或无法完整读取区域时，安装在 IPA 传输前被拒绝。该检查是 Autophone 为防范外部 Frida attach `SpringBoard` 残留而设的只读门禁（上游 `vphone-cli` 自身装包不依赖该探针；正常使用可选 Frida 能力时请只 attach 被测 App，勿 attach `SpringBoard`）。

没有 `procexp` 的上游 guest 可使用源码树的 `support/springboard-regions/springboard_regions.c`。在装有 Xcode iOS SDK 的 macOS 宿主上直接运行一键构建脚本：

```bash
scripts/build_guest_helpers.sh
```

脚本会编译并签名输出到 `dist/guest/autophone-springboard-regions`（也可传入自定义输出路径，内部封装了 `xcrun --sdk iphoneos clang -arch arm64` 与 `codesign --entitlements support/springboard-regions/springboard_regions.entitlements`）。运行 `autophone doctor --human` 可确认系统界面/定位状态以及是否已配置该可选探针。

通过 `autophone rig up --target <target.json> --springboard-region-probe "$PWD/dist/guest/autophone-springboard-regions"` 启动测试栈。Rig 启动器逐台通过 UDS 暂存探针，停止测试栈时清除暂存文件。手工部署可在 Rig Agent 或本地看板进程设置 `AUTOPHONE_SPRINGBOARD_REGION_PROBE`，值为对应 guest 中的绝对路径。guest 需要允许受信任的诊断程序读取 SpringBoard 的区域元数据。

## 可选 Guest AX 探针（wheel / sdist 安装）

五个 wheel / sdist 核心分发包仅包含纯 Python 代码与 JSON Schema，**不预置已编译的 iOS Mach-O 二进制**（对应产物路径在源码树中均为 gitignored）。仅使用 Vision OCR（`tap_text`、`assert_present` 等）时无需编译任何 AX 探针；若在独立 wheel / sdist 环境中启用 Accessibility（`tap_ax` / `AxSession`），请在装有 Xcode iOS SDK（`iphoneos`）的 macOS 宿主上从源码树本地编译，并通过环境变量指定产物路径：

| 可选 Guest 辅助产物 | 适用能力 | 一键编译脚本（源码树） | 默认产物路径（gitignored） | 运行时环境变量 / 参数 |
| --- | --- | --- | --- | --- |
| **SpringBoard 区域探针** | 第三方 IPA 安装前只读巡检（无 `procexp` 时） | `scripts/build_guest_helpers.sh` | `dist/guest/autophone-springboard-regions` | `--springboard-region-probe` / `AUTOPHONE_SPRINGBOARD_REGION_PROBE` |
| **`guest_ax_probe`** | `guest_axruntime` 进程外只读 AX 后端（**当前默认 AX 后端**，零 Frida 注入） | `scripts/build_guest_ax_probe.sh` | `support/guest_ax_probe`（附 `support/guest_ax_probe.build.json`） | `AUTOPHONE_GUEST_AX_PROBE` |
| **`ax_dump.dylib`** | `frida_inproc` 显式可选进程内 AX 后端与灰盒诊断 | `scripts/build_ax_dump.sh` | `support/ax_dump.dylib` | `AUTOPHONE_AX_DYLIB` |

### 1. 编译与配置 `guest_ax_probe`（默认 `guest_axruntime` 零 Frida 后端）

在 macOS 宿主源码根执行：

```bash
scripts/build_guest_ax_probe.sh /path/to/guest_ax_probe
```

脚本基于 `support/guest-ax-probe/ax_runtime_probe.m` 与 `support/guest-ax-probe/entitlements.plist`（单条最小权限 `com.apple.accessibility.api = true`，仅链接 `Foundation` / `CoreGraphics`）编译 `arm64 + arm64e` 通用 Mach-O 可执行文件并执行 ad-hoc + DER entitlement 签名，同时在同一目录写入带 SHA-256 摘要的 `<output>.build.json`。

在 wheel / sdist 独立安装环境中使用默认 `capabilities.ax.backend: guest_axruntime`（缺省值）时，为运行进程导出：

```bash
export AUTOPHONE_GUEST_AX_PROBE="/path/to/guest_ax_probe"
```

宿主加载器（`autophone/device/guest_ax_probe.py`）会在打开 `AxSession` 前核验文件存在性及同路径 `.build.json`（如存在）的 SHA-256 一致性，随后通过 UDS 按内容摘要缓存部署至 guest `/var/jb/tmp/autophone-guest-ax-probe-<sha16>`，不修改 `vphoned` 且无需安装 Frida。缺少文件或摘要过期时会 fail-closed 抛出 `AxUnavailable`。

### 2. 编译与配置 `ax_dump.dylib`（显式可选 `frida_inproc` 后端与灰盒诊断）

若在 `profile.yaml` 中显式选用 `capabilities.ax.backend: frida_inproc`（或需要进程内灰盒诊断如 `core_location_authorization`、`modal_sibling`），需安装 `frida` 可选依赖（`autophone[frida]` / `uv sync --group frida`），并在 macOS 宿主源码根编译 `support/ax-dump/ax_dump.m`（当前协议下限 `dumper_version: 9`）：

```bash
scripts/build_ax_dump.sh /path/to/ax_dump.dylib
```

随后在独立 wheel / sdist 运行进程中设置：

```bash
export AUTOPHONE_AX_DYLIB="/path/to/ax_dump.dylib"
```

未设置 `AUTOPHONE_AX_DYLIB` 时默认读取 `<CODE_ROOT>/support/ax_dump.dylib`；在未携带该编译产物的 wheel / sdist 环境中，打开 `frida_inproc` `AxSession` 前会 fail-closed 报错 `AxUnavailable`。

## 许可与测试数据

Autophone 的源码和五个核心分发包附带 Apache-2.0 许可证及 `NOTICE` 版权与上游致谢声明（致谢 [`vphone-cli`](https://github.com/Lakr233/vphone-cli) 提供的 iOS 虚拟化与 UDS/vsock 控制基础，其上游采用 MIT 许可证）。Python 和 Node.js 第三方依赖由安装工具从各自的索引获取，适用各自的许可。例如启用可选 `[postgres]` extra 时的 `psycopg-binary` 采用 LGPL-3.0-only，`certifi` 采用 MPL-2.0。若将来提供离线依赖包、容器镜像或已编译的 Frida 脚本，应对新增分发物单独核对许可和署名。 [Apache-2.0 许可文本](https://www.apache.org/licenses/LICENSE-2.0) · [Psycopg 元数据](https://pypi.org/project/psycopg-binary/) · [certifi 元数据](https://pypi.org/project/certifi/)

`VPhone.bundle`、iOS 固件、虚拟机镜像、签名凭据和被测 IPA 由部署者另行准备，适用其各自的许可和授权范围。Autophone 的 Apache-2.0 许可范围为随附的 Autophone 源码和文档。 [vphone-cli 上游文档](https://lakr233.github.io/vphone-cli/) · [Apple 软件许可](https://www.apple.com/legal/sla/)

使用有权限的设备和 App。截图、录制、剪贴板内容、定位和请求捕获可能包含个人数据或客户信息。共享回执前审查并按用途保留必要字段，原始文件保存在受限的本地状态目录。随包提供的定位路线和示例配置使用合成数据。看板默认绑定回环地址。通过反向代理开放时使用 TLS 和 `AUTOPHONE_AUTH`，代理访问日志省略查询字符串。嵌入式控制台启动凭证为短期单次凭证。
