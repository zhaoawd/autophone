# IPA 构建库与网页录制回放

**简体中文** | [English](packages-and-recording.en.md)

Autophone 将宿主侧 Package Store 作为 IPA 构建的唯一真源，通过 **Version Slot + 不可变 Build Record + Target 级安装回执** 保证每次测试可精确追溯到具体包体。

## 1. 创建 App Profile

先在可写的 App 资产目录中登记被测应用，并在运行看板或 CLI 时保持相同的 `AUTOPHONE_APP_ASSET_DIR`：

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/app-assets"
autophone app add example \
  --bundle-id com.example.app \
  --display-name "Example App"
```

## 2. IPA 入库与按 Build ID 精确安装

- **Version Slot**：同一个 `CFBundleShortVersionString` 对应一个稳定的 Slot；同版本多次构建记为同 Slot 下多条不可变 Build Record，各带唯一 `build_id`、SHA-256 摘要与可选源码提交身份。
- **Bundle ID 校验**：入库时解析 IPA 内 `Info.plist`，与 App Profile 不匹配即拒绝入库。
- **安装前 SpringBoard 检查**：安装前会核对 SpringBoard 进程身份与内存区域标签，若未安装 `procexp`，可使用 `scripts/build_guest_helpers.sh` 构建只读探针并通过 `autophone rig up --springboard-region-probe ...` 自动暂存（详见[核心分发安装与验证](../public-core.md#安装前检查)）。

命令行示例：

```bash
# 入库 IPA
autophone app package add example ./Example-1.2.0.ipa --label v1.2.0

# 查看 Slot、Build 列表与当前目标安装回执
autophone app package list example

# 按精确 Build ID 安装到指定 Rig
autophone app package install example --slot 001 --build-id <build_id> --device rig-a
```

也可在看板首页选择目标 Rig 后展开“**用例回归、IPA 与录制**”，直接上传 IPA、选择 Build 提交安装并查看已安装构建回执。

## 3. 网页录制、保存草稿与回放

网页录制与远程控制台共用设备 `ScreenLock`，保证操作序列可归因：

1. 在看板首页选定 Rig 与 App Profile，点击“**开始录制**”。
2. 在实时画面上点击、发送主屏幕按键或写入剪贴板，每个已确认回执的动作都会按顺序记录。
3. 点击“**完成录制**”，输入操作草稿名称并点击“**以录制步骤保存草稿**”。
4. 点击“**在所选 Rig 回放**”提交回放批次并查看 Run 结论。

> [!NOTE]
> 坐标级录制（`tap_xy`）依赖相同的分辨率与页面布局；进入长期回归资产时，建议将关键交互改写为 `tap_text`、`wait_text`、`assert_present` 等语义 Verb（见[编写用例与 Recipe](authoring-cases.md) 与 [Verb 参考手册](../reference/verbs.md)）。
