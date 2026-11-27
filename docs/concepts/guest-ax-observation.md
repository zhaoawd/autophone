# Guest 免侵入 AX 观测与双通道静态分治规格

> **文档状态**：Stage 3B 已落地、完成目标 iOS 26.1 VM 20/20 零 Frida 多应用长跑，并已正式晋升为默认 AX 后端（提交 `86825c4a7`：`app-profile.v1.json` 与 `DEFAULT_AX_BACKEND` 默认为 `guest_axruntime`，开箱零 Frida；`frida_inproc` 保留为显式可选后端 `capabilities.ax.backend: frida_inproc`，服务于 `api_capture`、`core_location_authorization`、`modal_sibling` 等进程内灰盒通道）。上承 [iOS App 受控操作与确定性验证架构蓝图](app-operation-language.md) 与 [统一 App 符号头文件（`app-symbols.v1`）最小契约规格](app-symbols-v1.md)，实施排期见 [受控操作与符号绑定分阶段实施任务书](app-symbols-v1-plan.md)，运行期硬门禁对齐 [回归运行期契约](runtime-contract.md) 与 [核心 JSON Schema 字典](../reference/schemas.md)，目标 iOS 26.1 VM 实证结论见 [实测验证报告](app-symbols-v1-validation-report.md)。

---

## 一、定位与演进边界：阶段一先落契约基线，阶段三实测并晋升为默认后端

围绕 iOS 可访问性树（AX）观测，本规格完整记录了**“阶段一（Round 1）先行端到端落地的契约基线”**到**“阶段三实测接入、20/20 零 Frida 长跑并晋升为默认 UI 后端”**的演进闭环：

### 1.1 阶段一建立并延续至今的两条核心契约规则
1. **Profile 声明 + 同一次 Run 静态固定单一 `ax_backend`**：
   - 在 `autophone/_data/schemas/app-profile.v1.json` 的 `capabilities.ax` 下声明 `backend` 字段（枚举 `frida_inproc | guest_axruntime`，当前默认 `guest_axruntime`）；
   - `ax_backend` 在 Run 启动期唯一确定并冻结到 `CaseContext` 与 `AxSession`，严禁在同一次运行中途按页面复杂度或负载动态互切后端；
   - **演进对照**：在**阶段一（Round 1）**，默认后端为 `frida_inproc`，尚未接入的 `guest_axruntime` 会在启动预检 fail-closed 拒止；在 **Stage 3B** 落地后，`guest_axruntime` 开放为显式 opt-in 后端；在完成 20/20 多应用零 Frida 长跑与两项跨 Run 复位前置项（`3ed53ebf6`）后，**当前（`86825c4a7`）**默认后端已正式晋升为 `guest_axruntime`，`frida_inproc` 保留为显式可选灰盒后端，其余未知或未支持的后端值仍于 Run 启动期严格 fail-closed 报错，绝不静默回退。
2. **快照、Run 证据与外部 Claims 端到端记录并校验 `ax_backend`**：
   - 进程内 `UIView` 遍历（`frida_inproc`）与系统跨进程 `AXRuntime`（`guest_axruntime`）在 React Native / SwiftUI 容器折叠、节点数量及顶层弹窗暴露语义上并非严格恒等；
   - AX 快照载荷、Run 证据及外部符号关系证据（`symbol.unique_visibility` 的 `scope.ax_backend`）必须显式记录产出时的 `ax_backend`；在核验 `unique_visibility` 证据复用与跨快照比对（`ax_comparison`）时，跨后端结论严格拒绝静默混用；
   - 同步修复 `autophone/_data/schemas/operation-ax-checkpoint.v2.json` 使其兼容 `build-identity.v2.json` 并允许记录 `ax_backend` 与 `snapshot_kind`。

### 1.2 默认切换收口与 `vphoned` 不侵入边界
- **默认 UI 后端已晋升，继续保持不修改 `vphoned`**：阶段三独立探针在目标 iOS 26.1 VM 通过四项实测（**STRONG GO**）、Stage 3B 完成 20/20 多应用零 Frida 交替长跑且补齐跨 Run `reset: relaunch` 回退与根页 `ready_markers` 后（`3ed53ebf6`），默认 UI AX 后端已正式切换为 `guest_axruntime`（`86825c4a7`）；探针继续以独立二进制按内容摘要缓存部署，仍不修改上游 `vphoned`。
- **后端演进的四项实测判据**（已在目标 iOS 26.1 VM 全部实测通过，数据见 [实测验证报告](app-symbols-v1-validation-report.md)）：
  1. **目标控件覆盖率**（尤其 React Native / SwiftUI 自定义容器下的 `identifier` 暴露率）；
  2. **弹窗与浮层遮挡判定能力**（当系统 Alert 或应用内弹窗遮住底层唯一按钮时，能否准确识别底层按钮不可点击，避免穿透误点）；
  3. **采集失败率与首次唤醒稳定性**（含零 `Tag 255` 内存残留与零 VoiceOver 核验）；
  4. **单次快照与端到端步骤延迟**。
- **触发记录**：阶段一 M6 真机弹窗遮挡实测（CE #6a）证实部分 React Native `<Modal>` 场景下进程内 `ax_dump-v8` 存在穿透误点，叠加反附加/反注入目标包的零注入观测需求，已如期触发阶段三独立探针实验、Stage 3B 接入与最终默认晋升。

---

## 二、双通道职责边界与禁止运行期互切的依据

在双后端架构下，Guest 进程外 AX 与 Frida 进程内探针按职责正交划分，不互作运行期动态回退：

| 通道标识 | 运行位置与侵入性 | 职责范围 | 启用方式 |
| :--- | :--- | :--- | :--- |
| **`guest_axruntime`**<br/>（当前默认 UI 后端） | Guest 进程外独立特权辅助程序；对被测 App **零注入、零 `Tag 255` 内存页** | 默认常规 UI 结构寻址、页面控件枚举，以及包含 `SpringBoard` 系统级弹窗/控制中心在内的跨应用顶层遮挡观测（`snapshot_kind="ui_structure"`） | `capabilities.ax.backend` 默认值（缺省即启用，纯 UI 场景零 Frida） |
| **`frida_inproc`**<br/>（显式可选灰盒探针） | `task_for_pid` 附加目标进程并加载模块/脚本；产生不可逆进程内驻留痕迹 | 跨进程 AX 物理不可达的进程内灰盒能力（如 `api_capture` 网络明文抓取、`core_location_authorization` 原生私有状态直读与 `modal_sibling` 进程内遮挡诊断，`snapshot_kind="inproc_graybox"`） | 在 `profile.yaml` 中显式声明 `capabilities.ax.backend: frida_inproc`（或由 `borrowed_lifetime` 传入）于 Run 启动期静态挂载 |

**禁止在单次 Run 中途按负载动态互切的原因**：
1. **冷附加与清理成本远高于单帧节省**：中途临时拉起 Frida 附加并在 Run 结束后按 `autophone/execution/app_frida_hygiene.py` 执行进程终止与冷重启，固定开销达秒级；长列表高耗时问题应通过向采集端下推 `--pid`、`--viewport-only`、`--max-depth` 剪枝解决。
2. **进程内存痕迹单向不可逆**：一旦中途切回 `frida_inproc`，`Memory Tag 255` 脏页在进程存活期内无法消除，带环境自检的目标包会在步骤中途崩溃。
3. **防止同一 Run 内唯一性语义漂移**：中途切换后端会导致同一页面在同一 Run 前后两次刷新的节点集合不一致，破坏 `require_unique` 判定的可解释性。

---

## 三、跨进程 `AXRuntime` 原理、安全红线与实测性能

iOS 原生辅助功能客户端（如 `XCUIApplication`）采用 **`外部特权进程 ↔ backboardd ↔ 目标进程系统内置 UIKit.axbundle`** 的三方 Mach IPC 模型：

1. **零第三方注入唤醒**：外部进程调用 `/usr/lib/libAccessibility.dylib` 的 `_AXSApplicationAccessibilitySetEnabled(1)` 与 `_AXSSetAutomationEnabled(1)` 后，目标进程内的系统 `UIKitCore` 自行加载 Apple 签名的 `/System/Library/AccessibilityBundles/UIKit.axbundle` 并暴露 AX 端口，无需注入第三方 dylib。
2. **跨进程批量拉取与遮挡观测**：通过 `/System/Library/PrivateFrameworks/AXRuntime.framework` 导出的 `AXUIElementCreateSystemWide`、`_AXUIElementCreateAppElementWithPid`（iOS 26.1 实测确认存在，而 `AXUIElementCreateApplication` 与 `_AXUIElementCreateWithPID` 不存在）、`AXUIElementSetMessagingTimeout` 及 `AXUIElementCopyMultipleAttributeValues` 批量读取节点树（属性号 `5019` 对应 `axIdentifier` / `accessibilityIdentifier` / RN `testID`），并结合 `systemApplication` 的锁屏、控制中心与 `primaryApp` 归属及跨进程坐标命中测试识别遮挡。
3. **安全红线：严禁开启 `VoiceOver`**：严禁调用 `_AXSVoiceOverTouchSetEnabled(1)`——该开关会接管 `backboardd` 全局手势路由（单击变焦点移动、双击才激活），直接破坏 `tap_xy` / `tap_text` / `tap_ax` 的物理点击契约。探针与宿主适配层每次采集均校验 `_AXSVoiceOverTouchEnabled() == 0`，异常时立即 fail-closed。
4. **目标镜像实测性能（对比立项预估）**：立项阶段按跨进程 Mach IPC 模型预估独立 CLI helper 单次快照为 `30–120 ms`（若远期合入常驻守护进程预估 `20–60 ms`，目前仍未改动 `vphoned`）。目标 iOS 26.1 VM 实测数据已落盘（完整对比见 [实测验证报告](app-symbols-v1-validation-report.md)）：
   - **独立探针直接采集（`S01_home`，N=20）**：免冷附加（`0 ms`），访客侧 P50/P95 为 **`56.23 / 59.24 ms`**，宿主端到端 P50/P95 为 **`66.50 / 69.95 ms`**（对比 `frida_inproc` 冷附加 `569.15 ms`、默认 `settle_s=0.8` 宿主 P50/P95 `811.68 / 816.69 ms`）；
   - **Stage 3B 完整 `AxSession.refresh()` 链路（`vm-2607`，N=10）**：含前台归属核验与 UDS shell 调度，端到端 P50/P95 为 **`289.84 / 416.67 ms`**（全程无 Frida attach/detach；Home 桌面与控制中心前台门禁短路拒止耗时 `149.90–165.56 ms`）。

---

## 四、Stage 3B 接入 `AxSession` 已对齐的 4 项代码契约

除阶段一已完成的 `ax_backend` 字段贯通与 `operation-ax-checkpoint.v2.json` 对 `build-identity.v2.json` 兼容修补外，Stage 3B 将 `guest_axruntime` 正式接入宿主 `AxSession`（沿用与 `autophone/device/lock_state_helper.py` 一致的 `/var/jb/tmp/` 按内容摘要缓存部署方式，编译脚本为 `scripts/build_guest_ax_probe.sh`，不触碰 `vphoned`）时，已在 `autophone/device/guest_ax_probe.py`、`autophone/device/ax_session.py` 与 `autophone/observation/impact/variant_evidence.py` 中落地以下 4 项契约对接（以及实测新增的强制 `--pid` 前台与挂起进程保护门禁）：

1. **`ax_checkpoint` 的构建版本字段补齐**：
   `autophone/observation/ax_checkpoint.py` 要求快照携带 `short_version` 与 `bundle_version`。`guest_ax_probe.py` 按“探针返回 -> 进程内缓存 -> UDS `app_list` -> UDS `file_get` 读取目标应用 `Info.plist`”顺序补齐版本字段；`AxSession.snapshot()` 缺失任一版本字段时严格 fail-closed 抛出 `AxUnavailable`。
2. **`variant_evidence.py` 的进程内字段静态分治**：
   `autophone/observation/impact/variant_evidence.py` 已按 `snapshot_kind` 将通用 UI 结构快照（`ui_structure`，`guest_axruntime` 产出）与进程内灰盒状态快照（`inproc_graybox`，`frida_inproc` 产出）显式分流：`ui_structure` 放行 `ax_ui_text` 等纯 UI 规则，对依赖进程内状态的 `core_location_authorization` 规则严格拒止（`variant_inproc_graybox_required`），且 `ax_comparison.compare()` 拒绝混用不同 `ax_backend` 的快照（`ax_backend_mismatch`）。
3. **首次激活的异步就绪退避与前台挂起保护**：
   首次置位 `_AXSApplicationAccessibilitySetEnabled(1)` 后，目标进程需在主线程下一轮 RunLoop 完成端口注册。探针与 `AxSession` 默认采用有界退避重试（默认 `max_retries=5`、`retry_delay_ms=150`），重试耗尽仍为空树时 fail-closed，不把未就绪空树当作有效快照返回；同时探针在调用 `_AXUIElementCreateAppElementWithPid` 前先行核验 `systemApplication` 的锁屏、控制中心与 `primaryApp.pid` 归属并立即短路，避免对 Home 后台挂起进程发起同步 Mach IPC 阻塞，并在非前台或遮挡场景下强制 fail-closed 报 `overlay_occluded`。
4. **屏幕几何与坐标对齐**：
   `autophone/device/ax_target.py` 依赖快照中的 `screen.{scale, w, h}` 换算 UDS 原生像素。独立探针仅链接 `Foundation` 与 `CoreGraphics`（零 `UIKit` 依赖与 RunLoop，不硬编码分辨率），宿主适配层通过 UDS `capabilities.screen` 交叉校验 `scale / w / h` 与屏幕原点，出现尺寸或缩放不一致时 fail-closed。

---

## 五、阶段三独立探针实证总结与复现清单

> [!IMPORTANT]
> **目标 iOS 26.1 VM 实证结论摘要（原立项三项待实证内容均已闭环，完整数据见 [实测验证报告](app-symbols-v1-validation-report.md)）**：
> 1. **最小 Entitlements 集合**：9 组权限消融实测确认唯一必要且充分的最小权限为单一键值 `com.apple.accessibility.api = true`（沉淀于 `support/guest-ax-probe/entitlements.plist`），无需 `platform-application`、`com.apple.private.accessibility.inspection` 或 `task_for_pid-allow`；
> 2. **目标固件 `AXRuntime.framework` 导出符号与属性号**：动态核验确认导出 `_AXUIElementCreateAppElementWithPid`（不存在 `AXUIElementCreateApplication` / `_AXUIElementCreateWithPID`）与 `AXUIElementSetMessagingTimeout`；`accessibilityIdentifier` / RN `testID` 对应属性号 `5019`（`axIdentifier`），而非内部数字 UID `2031`；
> 3. **实测覆盖率、遮挡、洁净度与延迟**：目标在屏可点控件覆盖率 100%、跨进程遮挡（部分 RN `<Modal>` 与控制中心）识别严格强于进程内探针、连续 50 次 0% 失败率、`Tag: 255` 脏页与 VoiceOver 严格为 `0`，四项综合裁决 **STRONG GO** 并已完成 Stage 3B 真机 E2E-1–E2E-4 闭环。

阶段三独立探针实验与 Stage 3B 端到端验真按以下三步完成（探针源码与编译脚本已沉淀于 `support/guest-ax-probe/` 与 `scripts/build_guest_ax_probe.sh`，完整数据见 [实测验证报告](app-symbols-v1-validation-report.md)）：
1. **符号与权限核验**：扫描目标 Guest 镜像 `dyld_shared_cache` 中的 `AXRuntime.framework` 与 `libAccessibility.dylib`，实测确认函数签名、属性号与最小 entitlement 集合。
2. **独立只读探针编译与按需部署**：通过 `scripts/build_guest_ax_probe.sh` 编译 `arm64 + arm64e` 通用二进制并自签名（`codesign --generate-entitlement-der`），由 `autophone/device/guest_ax_probe.py` 按内容哈希缓存部署至 `/var/jb/tmp/`，启用 Application Accessibility 与 Automation 开关、校验 VoiceOver 恒为 `0` 并执行前置前台门禁与首次唤醒退避。
3. **四项对比与 Stage 3B 端到端验真**：在目标 iOS 26.1 VM 上对系统 `Settings`、`Food Ordering`（含部分 `<Modal>` 与控制中心/Home 桌面遮挡场景）完成四项对比实测（目标控件覆盖率、弹窗/系统浮层遮挡识别、连续采集失败率与零 `Tag 255` 内存残留、P50/P95 延迟）及 Stage 3B `AxSession` / `tap_ax` / `ax_checkpoint` / `variant_evidence` 全链路实测。
