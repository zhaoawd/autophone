# 定位能力域

> 文档状态：**现行分域真源**（公开版）。本文只描述 `autophone/location/` 及相关入口的当前代码事实。
>
> 完成度以开发仓库的能力成熟度矩阵为准，本文不改判矩阵格子。
> 架构决策以
> [ADR-0005 统一定位会话，保留系统与 App 目标的独立运行时](../adr/0005-unify-location-sessions-keep-target-runtimes-separate.md)为准。

## 一、范围与不变量

### 1.1 范围

定位能力域覆盖：位置来源（fixed / approach / route / external stream / host）、位置目标
（系统 CoreLocation、App CoreLocation spawn-gate）、统一会话与所有权、路线 fixture 与回放算法、
route checkpoint、常用点注册表，以及对应的 HTTP API、页面与验收探针。

不覆盖：在线算路与地图匹配、绕过 iOS 定位权限、自动识别任意第三方定位 SDK、
到店/订单/ETA 等业务裁决。公开版不包含 App 级第三方定位 SDK 注入 target、App 自动定位策略、
Location Lease 路由和履约位置 guard。

### 1.2 不变量

| # | 不变量 | 代码位置 |
| --- | --- | --- |
| 1 | **系统 target 与 App target 不合并为一个运行状态机。** 系统 CoreLocation 是 device-scoped，同一 Device 同时只有一个系统位置来源，由 vphone-cli 拥有；App target 是 app-scoped，按 `(device, bundle_id)` 管理进程与消费证据。统一会话只做编排，不复制两类运行时的 heartbeat、guest reconnect、PID 观察或 Frida attach 实现。 | `coordinator.py`、`targets.py`、`system.py` |
| 2 | **四级事实分离**：`desired`（owner 想要什么）、`applied`（host/guest ACK 或 Adapter accepted receipt）、`observed`（App 回调或 UI 回读）、`business accepted`（业务请求坐标与响应）。`applied` 不能推出 `observed` 或 `business accepted`。 | `contracts.py`（`LocationTargetState`、`DeliveryReceipt`） |
| 3 | **required target 全部 ACK 后才 commit**；投递失败时冻结同一 proposal 原地重试，保持相同 producer sequence、时间戳和 payload，不跳点，不按断线时长补进度。 | `route_replay.py`、`sources.py`、`publishers.py` |
| 4 | **两类 sequence 分离**：`producer_sequence` 由来源分配；每个目标独立维护 delivery sequence。同序号同 payload 重试返回同一接受结果；同序号异 payload 与序号回退返回 `location_sequence_conflict`，缺帧返回 `location_sequence_gap`。 | `targets.py`（`_SequencedAdapter`）、`coordinator.py` |
| 5 | **坐标系显式，且只在投递 seam 转换一次**：坐标携带 `gcj02` 或 `wgs84`；权威样本保持来源坐标系，目标 Adapter 转换后投递；receipt 同时记录权威坐标与投递坐标。进入 vphone-cli host UDS 的系统定位请求固定为 WGS-84。 | `journey/coordinates.py`、`targets.py`、`system.py` |
| 6 | **generation 隔离旧生产者**：新建、替换或停止来源会生成新 generation；旧 generation 的控制和样本被拒绝。 | `system.py`；vphone-cli `SystemLocationController`（本仓外） |
| 7 | **不做隐式 fallback**：系统目标不是 App Adapter 失败后的后备，App Adapter 也不覆盖 Device 的系统位置状态；未注册的 target kind 返回 `location_target_unsupported`。 | `coordinator.py`（`production_location_target_factory`） |
| 8 | **首读前投递只对显式声明的 App 开放**：App CoreLocation spawn-gate 要求 App Profile 的 `location.core_location_spawn.allowed=true`，bundle 与坐标系必须与 Profile 一致。 | `targets.py`（`CoreLocationSpawnGateAdapter`）、`autophone/assets/apps.py` |
| 9 | **ScreenLock 期间暂停设备访问**：Device 被 ScreenLock 持有时，该 Device 上的会话暂停投递；锁释放后恢复。 | `coordinator.py`（`reconcile_access`、`await_quiesced`） |
| 10 | **持久化只覆盖已提交进度**：route 只保存 ACK 已提交的 checkpoint，恢复后保持 paused，由同一 owner 显式 resume；external stream 不持久。 | `checkpoints.py` |
| 11 | **route fixture 是正式回放的唯一几何输入**：在线地图响应只用于一次性采集，不能成为回归运行期依赖；总里程本地重算，不信任外部汇总字段；fixture 不含订单号、账号、设备身份、token、cookie 或原始地图响应。 | `route_replay.py`、`autophone/_data/routes/*.route.json` |
| 12 | **区分投递与消费证据**。Adapter accepted 或 guest ACK 记录投递结果。App 消费和业务接受需要单独观测。 | `contracts.py`、`autophone/adapters/http/static/platform.js` |

## 二、代码落点

### 2.1 `autophone/location/`

| 文件 | 职责 |
| --- | --- |
| `contracts.py` | 无副作用的共享契约：`CoordinateSystem`（`gcj02` / `wgs84`）、`LocationSourceKind`（`fixed` / `approach` / `route` / `external_stream` / `host`）、`LocationTargetKind`（`system_core_location` / `app_core_location_spawn`）、会话状态、控制命令、目标状态，以及坐标、样本、来源、目标、会话、receipt 和 snapshot 值模型。schema 为 `autophone.location-system/v1`。 |
| `coordinator.py` | `LocationSessionCoordinator`：会话启动、替换、发布、控制、停止与快照；目标声明权（claim）冲突检查；ScreenLock 访问协调；生产 target 工厂与 source 工厂。 |
| `targets.py` | 目标 Adapter seam：`LocationTargetAdapter` 协议、带序号校验的 `_SequencedAdapter`，以及三个实现（见 2.2）。 |
| `sources.py` | 受管来源 Driver：`RouteLocationSourceDriver`（路线回放，支持 checkpoint 与尾段）和 `ApproachLocationSourceDriver`（按 origin、destination、`speed_mps` 直线逼近）。 |
| `publishers.py` | `LocationStreamPublisher`：为单个目标 Adapter 管理 producer sequence 与重试身份；被拒样本保持 pending，重试必须使用相同 payload、序号和时间戳。 |
| `route_replay.py` | route fixture 校验与加载、内置路线目录、`RouteReplayEngine` 累计里程回放、尾段合成。 |
| `checkpoints.py` | `LocationCheckpointStore`：ACK 已提交进度的本地原子 checkpoint，默认路径 `config/location-checkpoints.local.json`，可用 `AUTOPHONE_LOCATION_CHECKPOINTS` 覆写，schema `autophone.location-checkpoints/v1`。 |
| `system.py` | `SystemLocationClient`：vphone device-scoped 系统定位协议的类型化 client，消息为 `location_source_set`、`location_stream_start`、`location_stream_push`、`location_source_control`、`location_source_status`、`location_source_stop`；默认经 UDS 传输。 |
| `system_sessions.py` | `SystemFixedLocationSession` 与 `SystemLocationStreamSession`：owned 系统 fixed 与 external stream 会话辅助；未传 `device` 时使用当前绑定设备。 |
| `session.py` | `LocationExecutionSession` 与 `DeviceSystemLocationAdapter`：Run 执行期经 `DeviceSession.set_location` 投递样本。 |
| `points.py` | 常用点注册表：seed `autophone/_data/location-points.yaml` ∪ 本机 overlay `<STATE_ROOT>/config/location-points.local.yaml`；`kind` 取 `custom` / `delivery` / `store`，新增 `kind=store` 被拒绝（门店由门店目录管理）。 |
| `journey/models.py` | 投递给 App Adapter 的样本与 ACK 契约（`LocationSample`、`DeliveredLocation`、`AdapterAck`）。 |
| `journey/coordinates.py` | 只在 Adapter 边界使用的确定性 WGS-84 / GCJ-02 / BD-09 转换。 |
| `journey/adapters.py` | 与 Frida 和设备实现无关的 `LocationAdapter` 协议。 |
| `adapters/corelocation_control.py` | CoreLocation 控制探针。调用方显式提供 Bundle ID。探针在恢复 App 进程前加载 Frida CoreLocation mock，记录 App 是否消费受控位置。`CoreLocationSpawnGateAdapter` 复用该探针。 |
| `adapters/journey_frida.py` | 常驻 Frida worker（`FridaWorker`）、进程守护（`AppProcessGuard`）与 `LocationAdapter` 实现。 |

### 2.2 目标 Adapter

| Adapter | target kind | 说明 |
| --- | --- | --- |
| `VPhoneSystemLocationAdapter` | `system_core_location` | 看板通过设备会话通道按设备路由到 Rig，在目标侧取得 ScreenLock 后访问 UDS。独立构造默认客户端时，`open` 要求会话 `device` 匹配当前绑定目标的设备键或 VM 目录名，并把该目标的 socket 固定到会话；不匹配时返回 `location_device_target_mismatch`，不访问设备，不回退默认 VM。 |
| `CoreLocationSpawnGateAdapter` | `app_core_location_spawn` | 一次性、operation 级目标：只接受 fixed 来源，spawn-gate 声明的 App，等待有界消费证据后在 `deliver` 返回前结束进程。不是后台常驻运行时。 |
| `FakeLocationTargetAdapter` | 任意 | 内存 Adapter，用于宿主隔离测试。 |

HTTP 面使用的 `LocationSessionCoordinator` 只登记 `system_core_location`；
`CoreLocationSpawnGateAdapter` 由调用方显式构造，不经 HTTP 默认工厂创建。

### 2.3 route fixture 与尾段

- 内置路线目录为 `autophone/_data/routes/`，文件名 `<route_id>.route.json`。公开版附带一条合成示例
  `example-demo-route-1p8km-driving.route.json`（约 1.8 km，`source.store_code=DEMO001`，坐标为示意值）。
- fixture schema 为 `autophone.route-fixture/v1`，`coordinate_system` 必须为 `GCJ-02`，`travel_mode` 必须为
  `driving`，`geometry` 为至少两点的 `LineString`。校验拒绝相邻点跳变超过 250 m、起终点与声明值偏差超过
  30 m、重复点比例异常和总长为零的几何。一个内置 fixture 无效时整个目录加载失败。
- route source 可携带 `tail_to`（GCJ-02）。fixture 终点样本经 required target 全部 ACK 后，Driver 按
  `12 m/s × time_scale` 推进到尾段终点；该合成直线段不属于 fixture 几何。`source_runtime` 中
  `route_distance_m` / `total_m` 只统计 fixture，`tail_distance_m` / `tail_total_m` 单独统计尾段，`phase`
  为 `route`、`tail` 或 `hold`。
- `arrive_now` 投递最终终点，ACK 后进入 holding。checkpoint 保存尾段终点与已提交尾段进度。

## 三、对外入口

### 3.1 HTTP API

路由定义在 `autophone/adapters/http/location.py`，业务逻辑在 `autophone/application/location.py`
（`UnifiedLocationApplication`）。

统一会话面：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/location/sessions` | 定位能力与会话只读模型（system snapshot） |
| `GET /api/location/devices/{device}/current` | 通过所选 Rig 向 guest 回读系统位置、采样时间和新鲜度 |
| `GET /api/location/routes` | 内置 route fixture 摘要 |
| `GET /api/location/checkpoints` | ACK 已提交的 route checkpoint 摘要 |
| `POST /api/location/checkpoints/{session_id}/restore` | 按原规格恢复 route；恢复后保持 paused，需同一 owner 显式 resume |
| `POST /api/location/sessions` | 创建会话（来源 + 一组目标）；fixed 来源必须带 `initial_sample` |
| `GET /api/location/sessions/{session_id}` | 会话快照 |
| `POST /api/location/sessions/{session_id}/samples` | 投递下一条权威样本；required target 全部 ACK 后才推进会话序号 |
| `PATCH /api/location/sessions/{session_id}` | `pause` / `resume` / `set_time_scale` / `arrive_now` / `hold` |
| `DELETE /api/location/sessions/{session_id}` | 停止会话 |

写操作必须携带 `owner`，缺失时返回 422（`location_owner_required`）。错误码到 HTTP 状态的映射：
会话不存在为 404；guest 不可用、协议错误、目标停止失败和 checkpoint 读写失败为 503；契约、来源、
目标、命令和控制值不合法为 422；owner 冲突、声明权冲突、序号冲突等其余领域错误为 409。

辅助面：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/vm/location/policies` | 公开版不内置 App 定位注入 provider，恒返回 `{"available": false, "running": false, "policies": []}` |
| `GET /api/vm/location/points` | 常用点列表，可按 `kind` 过滤 |
| `POST /api/vm/location/points` | 新增常用点（只写 overlay）；id 已存在返回 409 |
| `DELETE /api/vm/location/points/{point_id}` | 删除 overlay 常用点；seed 点返回 409，不存在返回 404 |

公开版没有 `/api/vm/location/lease` 路由和单 target 后台注入入口。

### 3.2 核心看板与 CLI

在核心看板选择 Rig 后打开“系统定位”。面板读取内置路线、会话和所选设备的系统位置，可启动定点和路线、暂停或停止会话。`autophone location current|set|route|stop --rig <Rig>` 通过相同的看板 API 操作。调用示例见[系统定位与路线回放](../guides/location.md)与 [README](https://github.com/zhaoawd/autophone/blob/main/README.md)。

### 3.3 CLI 验收

可通过以下命令运行定位验收。

```bash
autophone acceptance run --checks rig location \
  [--location-lat 31.2304 --location-lng 121.4737]
```

- `--checks` 的合法值为 `rig`、`regression`、`verification`、`location`
  （`autophone/adapters/cli/acceptance.py`）。
- `location` check 在 `rig` 未通过时记为 `skipped`，不进入设备操作（`autophone/application/acceptance.py`）。
- 判据：以 WGS-84 坐标创建一个 fixed 来源、单个 `system_core_location` 目标的会话；会话状态为
  `running` 且 `last_producer_sequence=0` 时通过，随后停止会话并把停止结果写入证据。该判据只证明
  系统目标 applied，不证明 App 或业务消费。

## 四、已验证范围

- 宿主隔离测试覆盖共享合同、Coordinator、目标 Adapter、来源 Driver、route fixture 校验、checkpoint、
  HTTP 路由与页面（`tests/test_location_*.py`、`tests/test_system_location*.py`、
  `tests/test_platform_location*_api.py`、`tests/test_route_replay.py`）。
- 系统 target 的 fixed / stream / route、route pause/resume、reconnect/restart/stop 和 CoreLocation App
  回读历史上已在活机验证。活机结论与日期以能力矩阵为准，不外推为逐样本 App 消费或业务接受。
- vphone-cli 侧 `SystemLocationController` 与 guest 定位实现位于 `$VPHONE_CLI_DIR`，不在本仓。
