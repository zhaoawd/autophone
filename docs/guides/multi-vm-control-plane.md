# 多 VM 控制面与隔离部署指南

**简体中文** | [English](multi-vm-control-plane.en.md)

当需要在同一台 macOS 宿主上管理多台 `vphone` 虚拟机、实现按 Rig 隔离派单、并发屏幕锁管理或故障隔离时，可启用基于 PostgreSQL 的 **Control Plane**。

## 1. 为每台 VM 生成 `DeviceTarget`

使用 `autophone rig init` 为每台 VM 生成独立的 16 字段 `DeviceTarget` JSON（自动读取 `udid-prediction.txt` 并递增分配宿主 SSH/Frida 端口，防止多机端口冲突）：

```bash
# 第一台 VM（默认 SSH 2222 / Frida 27042）
uv run --group platform autophone rig init rig-a \
  --bundle /absolute/path/to/VPhone.bundle

# 第二台 VM（自动避开已有 target.json 端口，例如 SSH 2223 / Frida 27043）
uv run --group platform autophone rig init rig-b \
  --bundle /absolute/path/to/VPhone.bundle
```

生成的配置位于 `~/.vphone/machines/<rig-id>/autophone/target.json`，每台 Rig 拥有独立的 `vm_dir`、`vphone.sock`、`runtime_dir`、`log_dir`、`temp_dir`、`package_cache` 与宿主端口。

## 2. 一键拉起多 VM 控制面栈

### 方式 A：使用已有 PostgreSQL（推荐用于持续运行）

```bash
export AUTOPHONE_DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/autophone_control"
uv run --group platform autophone rig up \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
  --target "$HOME/.vphone/machines/rig-b/autophone/target.json"
```

### 方式 B：使用临时本机 PostgreSQL（适合快速多机体验）

若本机已安装 PostgreSQL 工具链（`pg_config`、`initdb`、`pg_ctl`），可传入 `--ephemeral-db` 自动在临时目录启动 Unix-socket 数据库并在退出时自动清理：

```bash
uv run --group platform autophone rig up --ephemeral-db \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
  --target "$HOME/.vphone/machines/rig-b/autophone/target.json"
```

### 可选参数

- `--control-port`（默认 `8100`）与 `--dashboard-port`（默认 `8000`）：覆写监听端口。
- `--allow-multi-vm`：宿主上已有其他 VM 在运行时显式允许并行启动。
- `--springboard-region-probe <path>`：在每台上游 Rig 上通过 UDS 暂存只读 SpringBoard 区域探针（退出时自动清理），供第三方 IPA 安装前检查使用；可通过 `scripts/build_guest_helpers.sh` 编译生成。
- `--decline-usb-trust`（及 `--usb-trust-wait`，默认 `25` 秒）：在 VM 与受管控制台就绪后，按 guest `kern.bootsessionuuid` 每开机周期检查一次 iOS USB 配对弹窗并在安全匹配到 `"Trust This Computer?"` / `"Don't Trust"` 后自动点击拒绝（单机模式与控制面模式均支持；也可通过在目标 `<runtime_dir>/decline-usb-trust` 放置空标记文件按 Rig 启用）。

## 3. 隔离与 Fail-Closed 保障

- **目标隔离**：在看板首页切换目标 Rig 时，截图、远程控制台、系统定位、IPA 安装与用例 Run 均显式绑定到所选 `rig_id`；单台 Rig Agent 退出后，其余在线 Rig 继续提供服务。
- **不静默降级**：一旦配置了 `AUTOPHONE_DATABASE_URL`，若控制面或数据库不可达，CLI 与 HTTP API 一律 fail-closed 拒绝执行（CLI 返回退出码 `2`，HTTP 返回 `503`），**不会静默绕过控制面直连设备**。
- **内网安全**：仅绑定回环地址 `127.0.0.1` 时免鉴权；向局域网暴露前必须配置 `AUTOPHONE_AUTH="user:password"` 并配合 TLS 反向代理。

## 4. 手动分进程部署与生命周期管理

如需保留已有运行中的 VM 或分别托管各进程，可使用底层独立入口：

```bash
# 单独检查/启停指定 Target
uv run python -m autophone.rig.lifecycle status --target /path/to/target.json
uv run python -m autophone.rig.lifecycle start  --target /path/to/target.json --allow-multi-vm
uv run python -m autophone.rig.lifecycle stop   --target /path/to/target.json

# 分别启动控制面、Rig Agent 与看板
autophone-control
autophone-rig-agent --target /path/to/rig-a.json --capability vm_uds --capability app_install
autophone serve --host 127.0.0.1 --port 8000
```

另见[设备控制台与嵌入](device-console.md)、[系统定位与路线回放](location.md)与[核心分发安装与验证](../public-core.md)。
