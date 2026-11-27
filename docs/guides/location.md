# 系统定位与路线回放指南

**简体中文** | [English](location.en.md)

> 完整能力域设计与状态机说明见[定位能力域](../domains/location.md)及 [ADR-0005](../adr/0005-unify-location-sessions-keep-target-runtimes-separate.md)。

Autophone 支持按 Rig 隔离的系统 CoreLocation 定点设置与内置路线回放。使用前请在目标 iOS guest 中开启“**设置 → 隐私与安全性 → 定位服务**”。

- 定点与路线控制要求 guest 具备 `location_owned` 能力。
- 读取系统当前位置要求 guest 具备 `location` 能力。
- 已有定位会话由创建时的同一 `owner` 暂停、恢复或停止，防止多方互相覆盖。

## 命令行操作（推荐）

如看板配置了 `AUTOPHONE_AUTH`，在 CLI 环境中设置 `AUTOPHONE_DASHBOARD_AUTH="user:password"`：

```bash
# 读取指定 Rig 的系统当前位置
autophone location current --rig rig-a

# 创建系统定点会话（WGS-84）
autophone location set --rig rig-a --owner operator --session-id demo-fixed \
  --latitude 37.332 --longitude -122.031

# 停止定点会话
autophone location stop --rig rig-a --owner operator --session-id demo-fixed

# 按内置路线回放（默认每点经宿主回执确认后再推进）
autophone location route --rig rig-a --owner operator --session-id demo-route \
  example-demo-route-1p8km-driving
```

## HTTP API 操作示例

以下示例通过看板 HTTP 接口完成路线回放、系统回读与停止（将 `rig-a` 替换为实际登记的 Rig ID）：

```bash
base=http://127.0.0.1:8000

# 查看内置路线、活动会话与当前回读
curl --fail "$base/api/location/routes"
curl --fail "$base/api/location/sessions"
curl --fail "$base/api/location/devices/rig-a/current"

# 创建内置路线回放会话
curl --fail -X POST "$base/api/location/sessions" \
  -H 'Content-Type: application/json' \
  -d '{
    "spec": {
      "session_id": "route-demo",
      "owner": "platform-demo",
      "device": "rig-a",
      "source": {
        "source_id": "demo-route",
        "kind": "route",
        "coordinate_system": "gcj02",
        "route_id": "example-demo-route-1p8km-driving",
        "time_scale": 1
      },
      "targets": [
        {
          "target_id": "system:rig-a",
          "kind": "system_core_location",
          "coordinate_system": "wgs84"
        }
      ]
    }
  }'

# 读取 guest 系统回读并停止会话
curl --fail "$base/api/location/devices/rig-a/current"
curl --fail -X DELETE "$base/api/location/sessions/route-demo" \
  -H 'Content-Type: application/json' \
  -d '{"owner": "platform-demo"}'
```

## 观测边界

系统回读返回的坐标、`fresh` 与 `simulated` 来自 guest 系统定位服务。会话的期望坐标、宿主投递确认与系统回读应分别核对；验证特定 App 消费了定位结果需依赖该 App 的独立观测。
