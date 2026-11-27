# Autophone 测试环境与能力 FAQ

**简体中文** | [English](faq.en.md)

> 文档状态：**现行用户说明**（公开核心版）。
> 适用范围：Autophone 采用的越狱 iOS 虚拟机（`vphone-cli`）方案。具体 App、系统版本和场景的支持情况，以对应构建的验证结果为准。

Autophone 选择 `vphone`，是为了在同一个环境中运行兼容的 iOS 设备版 App、确定性控制测试过程并采集运行证据。Xcode Simulator 适合开发调试和模拟器构建测试；实体 iPhone 用于验证真实硬件和对应系统条件下的行为。三者的结果具有不同适用范围。

## 环境选择

### 1. `vphone` 与 Xcode Simulator、实体 iPhone 有什么区别？

| 比较项 | Xcode Simulator | Autophone + `vphone` | 实体 iPhone |
| --- | --- | --- | --- |
| 运行环境 | 面向模拟器的运行时，使用 Mac 内核 | 独立客机，使用经过适配的 iOS 固件 | 设备硬件及其安装的 iOS |
| App 构建 | 面向 iOS Simulator 编译 | 兼容的 iOS 设备版 IPA | 满足签名与系统要求的 iOS 设备版 App |
| 主要用途 | 开发调试、界面适配、单元测试和常规 UI 测试 | 指定构建的确定性业务流程回归、多 VM 控制面与运行观测 | 真实硬件、性能、功耗及对应系统条件下的验证 |
| 主要限制 | 不能直接替代指定设备版 IPA 的测试，也不复现完整硬件行为 | 存在虚拟硬件、固件修改和越狱造成的环境差异 | 进程观测与运行时插桩受权限限制；需要管理物理设备 |

参考：[Apple Simulator 机制（WWDC19）](https://developer.apple.com/videos/play/wwdc2019/418/) · [Running Your App on Simulated or Physical Devices](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices)

### 2. 为什么选择 `vphone`，而不是直接使用 Xcode Simulator？

Autophone 围绕**指定的设备版 IPA** 管理测试对象，并将每次 Run 的结果关联到具体 Build ID、包体哈希和目标设备的安装观测。即使源码相同，Simulator 构建也是不同的编译产物，不能直接替代设备版 IPA 的回归验证；当团队只持有设备版 IPA 时，差异尤为关键。

同时，越狱 `vphone` 提供了 UDS 控制通道、系统定位、进程检查、Frida 观测与宿主侧文件/运行目录管理条件。团队若拥有源码且目标主要是开发期调试，Xcode Simulator 依然是更轻量的选择。详见[IPA 构建库与录制回放](../guides/packages-and-recording.md)与 [Apple TN3117](https://developer.apple.com/documentation/technotes/tn3117-resolving-build-errors-for-apple-silicon)。

### 3. 既然测试设备版 IPA，为什么不直接使用实体真机？

普通真机对进程附加、区域检查、系统定位会话管理与文件访问存在签名和权限限制；越狱 `vphone` 能在单台 macOS 宿主上通过 UDS 与统一 Control Plane 管理多台隔离的虚拟机、运行目录与屏幕锁。

但若测试目标是未越狱环境、真实相机/传感器/基带行为、性能功耗或发布前机型兼容性，必须使用对应实体真机。`vphone` 结果不能替代这些验证。

### 4. 三种环境应该怎样配合？

| 测试目标 | 适用环境 | 结果范围 |
| --- | --- | --- |
| 开发调试、布局适配、单元测试与常规 UI 测试 | Xcode Simulator | 对应模拟器构建与运行时 |
| 指定设备版 IPA 的业务流程确定性回归与运行观测 | Autophone + `vphone` | 已验证的构建、虚拟机环境和用例集 |
| 真实相机、传感器、无线连接与硬件安全能力 | 实体 iPhone | 实际设备、系统版本和实验条件 |
| 性能、功耗、发热与发布前关键业务确认 | 实体 iPhone | 实际测量的设备与负载 |

## App 接入与兼容性

### 5. 任意 IPA 都能在 `vphone` 上运行吗？

不能保证。设备版 IPA 仍可能受 iOS 系统版本、签名与加密方式、硬件依赖、服务端校验以及越狱/虚拟环境检测影响。接入时应按安装、启动、关键流程与观测能力逐项验证。

### 6. 使用 Autophone 必须提供 App 源码吗？

对于兼容、可安装的 IPA，界面自动化、截图、OCR/AX 定位与系统定位不依赖源码。但若涉及代码级诊断、构建溯源或自定义观测适配，仍需要对应构建材料或研发配合。

### 7. 都是 ARM64，为什么设备版 IPA 不能直接跑在 Apple Silicon Simulator 上？

CPU 架构相同不代表目标平台 ABI 与系统框架一致。iOS 与 iOS Simulator 属于不同的构建目标（见 [Apple TN3117](https://developer.apple.com/documentation/technotes/tn3117-resolving-build-errors-for-apple-silicon)）。

### 8. 系统定位、摄像头、推送与支付都能测试吗？

需按功能和 App 分别确认。公开核心提供系统 CoreLocation 定点与路线回放（见[系统定位操作指南](../guides/location.md)），但注入测试坐标不代表验证了真实 GNSS 接收；依赖真实安全芯片、推送证书或支付环境的流程需另行在真机验证。

## 测试结果与 AI 边界

### 9. Autophone 是否让 AI 每次临场操作并判断测试通过？

**不是。** 正式回归运行期不依赖 `autophone.application.ai` 临场决策。AI 仅用于创建期的探索、操作起草和运行后的失败诊断，产出的资产均以 `draft` 进入校验与转正流程。正式回归按确定性 Typed-IR Recipe 和明确断言给出 Verdict，AI 诊断不能改写测试结论。详见[回归运行期契约](../concepts/runtime-contract.md)。

### 10. 如何证明测试的是指定构建，而不是版本号相同的另一个包？

平台通过不可变的 Build Record、IPA SHA-256、Build ID 与设备安装回执绑定测试对象，并在 Run 提交时冻结构建身份。详见[IPA 构建库与录制回放](../guides/packages-and-recording.md)。

### 11. 测试失败是否一定说明 App 有缺陷？

不一定。平台严格区分 `failed`（断言不符）、`blocked`（设备、资产或前置能力未就绪）与 `error`（协议、超时或编排异常），并在结果与报告中记录阶段与阻断码。详见[回归运行期契约](../concepts/runtime-contract.md)。

### 12. 虚拟机里的测试会影响真实业务吗？

**可能。** 虚拟机只隔离设备本地状态，不自动隔离 App 连接的服务端。连接真实后端时，下单、修改账号或发送消息会产生真实业务影响，接入前应明确测试环境、账号与数据清理策略。

### 13. 一台 Mac 能同时运行多少台 `vphone`？

无统一固定上限，取决于宿主内存、压力级别、虚拟机配置与负载特征。单台设备上的触屏操作由 `ScreenLock` 串行保护；横向扩展并发依赖多台独立 Rig 与 Control Plane 调度。详见[核心术语](../concepts/terminology.md)与[多 VM 控制面部署](../guides/multi-vm-control-plane.md)。
