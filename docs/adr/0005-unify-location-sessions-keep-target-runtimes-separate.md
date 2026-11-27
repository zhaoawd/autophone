# 统一定位会话，保留系统与 App 目标的独立运行时

> **Status: accepted.** 共享合同、`LocationSessionCoordinator`、system target Adapter、CoreLocation spawn
> target Adapter、四类来源、统一会话 API、route checkpoint 和验收探针已在 `autophone/location/` 实现。
> system target 的 fixed/stream/route、route pause/resume、reconnect/restart/stop 和 CoreLocation App 回读
> 历史上已在活机验证（2026-08-10）。App 级第三方定位 SDK target 不包含在公开版中。

平台使用一个 `LocationSession` 表达“一个位置来源向一个或多个目标发布样本”，由
`LocationSessionCoordinator` 统一管理 owner、lease、producer sequence、ACK barrier、状态和证据。
系统 CoreLocation 与 App 定位注入不合并为一个运行状态机：前者由 vphone-cli 按 Device 管理单一来源、
heartbeat 和 guest reconnect，后者由 Autophone 按 `(device, bundle_id, provider)` 管理 PID、Frida
session 和消费证据。统一会话可以支持同源多目标，同时保留两类目标不同的物理作用域和生命周期。

考虑过的另一方案是扩展现有单 target `LocationKeeper`，让它同时管理系统 UDS、多个 App、路线和业务
guard。该方案会把 Device 单例、per-process session、ScreenLock、guest reconnect 和 App relaunch 放入同一
状态机，并保留现有调用方的重复生命周期。目标设计使用统一外部会话接口和多个内部 Adapter；兼容入口只
翻译参数，不建立新的线程或持久状态。

结果是系统 ACK、App 消费和业务接受必须分别记录；系统目标不能作为 App Adapter 失败后的隐式 fallback；
路线只在所有 required target 接受同一个 proposal 后推进。
