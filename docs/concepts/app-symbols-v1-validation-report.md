# `app-symbols.v1` 与 Guest AX 实测验证报告（Round 1 / Round 1.5 / Stage 2 / Stage 3）

> **文档状态**：实证验证汇总（目标环境：`legacy` iOS 26.1 `23B85` 与 `upstream_v1` iOS 27.0.1 `24A446` `vphone-cli` 虚拟机）。本报告汇总 [统一 App 符号头文件最小契约](app-symbols-v1.md)、[受控操作与符号绑定分阶段实施任务书](app-symbols-v1-plan.md) 与 [Guest 免侵入 AX 观测与双通道静态分治规格](guest-ax-observation.md) 在 iOS 26.1 VM（Round 1 / Round 1.5 / Stage 2 联合裁决 9/9 / Stage 3 独立探针与 Stage 3B 多应用零 Frida 20 轮长跑）以及 `upstream_v1` iOS 27.0.1（`24A446`）VM 上的全阶段真机实测结果，供工程演进与后端选型决策引用。

---

## 一、实验环境与基线约束

- **目标设备**：运行中的 iOS 26.1 `vphone-cli` 虚拟机（实验前后均核验 `ScreenLock` 空闲、UDS 链路健康、SpringBoard 内存巡检 `verdict: "clean"`、`0` 个 `Tag: 255` 脏页）。
- **被测对象**：
  - `Food Ordering` React Native 示例应用的 3 个构建变体（共享完全相同的 `Info.plist` 版本元组 `CFBundleIdentifier="com.autophone.foodorderingdemo.v2"`、`CFBundleShortVersionString="1.0.0"`、`CFBundleVersion="1"`）；
  - 系统 SwiftUI 应用 `Settings`（`com.apple.Preferences`）与 `SpringBoard` 控制中心（Control Center）。
- **安全红线**：全过程严禁调用 `_AXSVoiceOverTouchSetEnabled`；Stage 3 每次探针运行前、中、后均核验 `_AXSVoiceOverTouchEnabled() == 0`。

---

## 二、Round 1（M6）：L2 双版本对照、L1 盲测双分母与 6 项反例实证

### 2.1 三版同版本号制品与不可变符号绑定（D2 / D5）

三版制品均保持 `1.0.0 (1)` 版本元组不变，通过 Package Store 记录精确摘要并安装至目标 iOS 26.1 VM：

| 制品角色 | `CFBundle` 版本 | `build_id` | `artifact_sha256`（前 12 位） | 差异内容 | 对应 L2 符号绑定与 `binding_sha256`（前 12 位） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **v1（基线）** | `1.0.0 (1)` | `bld-4f56b8da9d8c...` | `14282f675363` | 基线 `main.jsbundle`（`home.ai_prompt.preferences` 文案 `"调偏好"`） | `14282f675363.yaml` / `be2a2b581514` |
| **v2（改文案不改 ID）** | `1.0.0 (1)` | `bld-6b7a88a4748b...` | `e066c6f9972d` | 仅将 `home.ai_prompt.preferences` 可见文案由 `"调偏好"` 改为 `"口味设置"`，保留 `testID` | `e066c6f9972d.yaml` / `4ae6d4d97bf7` |
| **modal-probe（遮挡探针）** | `1.0.0 (1)` | `bld-d8344c513b0a...` | `56a04f08f559` | 触发真实 React Native `<Modal transparent={true}>` 覆盖 `home.ai_prompt.preferences`，同时保留页眉锚点 `"今晚，好好吃一顿"` 可见 | `56a04f08f559.yaml` / `be550f5f4de3` |

- **D2 不可变性实证**：当 `build_identity.probe(...)` 面对同版本号新包返回 `identity_state: "ambiguous"` 时，`record_unique_visibility_claim` 确定性 fail-closed 抛出 `SymbolClaimError`；完成构建标记核验（`identity_state: "confirmed"`）并向外部 `claims.json` 追加 `symbol.unique_visibility` 证据后，原始符号文件 `14282f675363.yaml` 的 `binding_sha256` 逐字节保持 `be2a2b58151431aaddd0f20179c51dc90a1524e24600b6b688486a8dea6f70d5` 不变。
- **示例资产校准**：真机 Vision OCR 与 AX 快照发现离线 `examples/food-ordering` 旧锚点（`今日推荐`、`口味偏好`）与真实 v1 包存在漂移，已据此校准为实测稳定锚点（`S01_home`：`今晚，好好吃一顿` / `立即下单`；`S02_preferences`：`饮食偏好` / `保存偏好`）。

### 2.2 L2 两版本执行对照与维护成本归因

| 用例 / 检查项 | Recipe 类型 | v1（`14282f675363`，`"调偏好"`） | v2（`e066c6f9972d`，`"口味设置"`，同 `1.0.0 (1)`） | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **`R2`（手写文案基线）** | `tap_ax(query="调偏好", role="button", screen_id="S01_home", require_unique=True)` | **PASS**（`1` 次 UDS 点击 -> `饮食偏好`） | **FAIL**（`locator_missing`，**`0` 次 UDS 点击**） | 文案改名即断，需逐一修改 Recipe |
| **`R3`（符号绑定）** | `tap_ax(symbol="ctrl.home.preferences")` -> `assert_present(["饮食偏好"])` | **PASS**（`1` 次 UDS 点击 -> `饮食偏好`） | **PASS**（`1` 次 UDS 点击 -> `饮食偏好`，**`0` 处 Recipe 修改**） | 自动路由至 `e066c6f9972d.yaml` |
| **`R4`（独立旧文案断言）** | 断言 v1 旧按钮文案 `"调偏好"` | **PASS** | **FAIL**（`locator_missing`，**`0` 次 UDS 点击**） | 证明“定位稳定性”与“文案预期校验”解耦（CE #1） |
| **`R3` 负对照（`skip_step`）** | 跳过 `tap_ax` 留在 `S01_home` -> `assert_present(["饮食偏好"])` | **FAIL**（`assertion_failed`）-> `counted=True, discriminator_flipped=True, teeth_at="recipe"` | 复用 v1 探针转正 v2 -> **`ready=False`, `discrimination_stale`** | 证明 CE #3 精确制品新鲜度门禁生效 |

- **维护成本归因拆分**：相对文案定位，“改文案不改 ID”的零修改收益来自**稳定 `identifier` 层**；**符号绑定层**的增量价值体现为：ID/区域/页面重命名时的单点扇出更新、执行前离线 `case lint` 断链拦截、集中式 `effects.{verbs, completeness}` 治理，以及 `promotion.py` 基于 `(artifact_sha256, binding_sha256)` 的精确新鲜度失效。

### 2.3 六项反例（CE #1–#6）实测矩阵

| 编号 | 反例场景 | 离线单测门禁 | 目标 iOS 26.1 VM 真机实证 | 关键观测事实 |
| :--- | :--- | :--- | :--- | :--- |
| **CE #1** | 仅改按钮文案（`"调偏好"` -> `"口味设置"`），保留 `home.ai_prompt.preferences` | PASS | **VERIFIED LIVE** | v2 OCR/AX 确认新文案；`R3`（0 修改）PASS（1 tap），`R2`/`R4` FAIL（`locator_missing`，0 tap） |
| **CE #2** | 同 `identifier` + `role` 出现 `0 / 1 / 2` 命中 -> `0 / 1 / 0` 次 UDS 点击 | PASS | **离线 2 命中 + 真机 0/1 命中** | 真机唯一命中发 1 tap；v2 旧文案 0 命中发 0 tap；2 命中 `ambiguous_match` 0 tap 由 M3 单测锁定 |
| **CE #3** | 同版本号 `1.0.0 (1)` 换包（v1 vs v2）或改 `binding_sha256` | PASS | **VERIFIED LIVE** | `has_reusable_unique_visibility`: `v1=True`, `v2=False`, `guest_axruntime=False`；v1 负对照探针校验 v2 判 `discrimination_stale` |
| **CE #4** | `skip_step` + 公共弱断言（`"微辣"` 同时存在于首页与偏好页） | PASS | **VERIFIED LIVE** | 正向与 `skip_step` 负向均 PASS -> `discriminator_flipped=False, counted=False` -> `promotion` 阻断（`discrimination_toothless`） |
| **CE #5** | 多副作用集合、拒绝 `"irreversible"` 通配符与 `lower_bound`/`unknown` 隔离试跑门禁 | PASS | **离线全覆盖 + 真机 `exhaustive` 实跑** | 真机 `R3` 在 `active` 模式通过 `exhaustive` 校验；M4 覆盖多动词与 4 条件草稿隔离门禁 |
| **CE #6a** | `frida_inproc`（`ax_dump-v8`）顶层弹窗遮挡 | PASS（合成） | **VERIFIED LIVE（触发 Round 1.5 与 Stage 3）** | 全屏控制中心被 `screen_id` 拦住，但无 `screen_id` 穿透；**部分 RN `<Modal>` 留出首页锚点时 `ax_dump-v8` 发生真实穿透误点**，且后续非符号 `assert_present` 失败触发 `_dismiss_blocking_overlay_once` 误关弹窗 |
| **CE #6b** | 页面结构合法（`S02_preferences`），业务实体数据不符 | PASS | **VERIFIED LIVE** | `R5` 导航至合法 `S02_preferences` 后，在独立业务预期校验处确定性失败（`fail_kind="assertion_failed"`） |

### 2.4 同包 L1 RN 盲提取双分母覆盖率实测

对真实 v1 `FoodOrdering` IPA 的 `main.jsbundle` 执行 `autophone app symbols extract-l1-rn`，并与全量可达页面（`S01_home`、`S02_preferences`、空 `Cart`）的真实 `frida_inproc` AX 快照交叉比对（按 `enabled_only=True` + 唯一命中统计）：

| 指标 | 计算公式 | 真机实测值 | 说明 |
| :--- | :--- | :--- | :--- |
| **L1 静态提取候选总数** | `main.jsbundle` 静态字面量 `testID` / `accessibilityIdentifier` | **31** | 13 个业务 ID + 18 个框架/Sentry/路由噪声 ID |
| **真机唯一可点命中数** | `extracted ∩ live_unique_clickable_ax` | **1**（`home.ai_prompt.preferences`） | 放宽 role 约束后仍为 `1` |
| **候选可用率（Candidate Usability Rate）** | `1 / 31` | **3.23%**（`0.0323`） | 30 / 31 候选从未唯一命中任何真机 AX 叶子节点 |
| **业务目标覆盖率（Business Target Coverage）** | `1 / 15`（独立列出的 15 个必测业务控件） | **6.67%**（`0.0667`） | 14 / 15 核心业务控件被静态字面量扫描漏检 |

**漏检与噪声根因（基于实测证据）**：
1. **58.1%（18 / 31）为第三方/错误边界噪声**（`sentry-feedback-*`、`router_error_*`、`sitemap-item*` 等），在业务主路径中永不渲染；
2. **35.5%（11 / 31）为非叶子容器或显式 `accessible={false}` 包装层**（如 `home.root`、`preferences.header.back_button` 等），iOS Accessibility 树会折叠忽略；
3. **3.2%（1 / 31）为瞬态保存态控件**（`verification.commit_save`）；
4. **14 / 15 业务控件漏检根因**：真机三页 AX 实际暴露了 **40 个唯一带 ID 控件**，但源码通过封装组件属性（`ObservedPressable(stableId="...")` 动态转发为 `testID: t`）与模板字符串（`` `preferences.spice.${e.value}` ``）生成 ID，静态字面量正则无法还原。该结果直接验证了 Track A（L2 构建清单优先于 L1 盲扫）与双分母报告的必要性。

---

## 三、Round 1.5：`ax_dump-v9` 遮挡加固真机验证（5/5 全绿，零误报）

针对 Round 1 CE #6a 暴露的 4 项真实穿透漏洞，Round 1.5 在 `support/ax-dump/ax_dump.m`（升级至 `dumper_version: 9`、`scripts/build_ax_dump.sh` 编译 `arm64 + arm64e` 通用动态库）与 `tap_ax` / `recipe.py` 中落地四层加固，并在目标 iOS 26.1 VM 完成全部 5 项真机闭环验真：

| 验证项 | Round 1.5 加固范围 | 目标 iOS 26.1 VM 实测结果 | 状态 |
| :--- | :--- | :--- | :---: |
| **Check 0** | 零误报基线（`ax_dump-v9` + v1 `R3`） | `S01_home` **`25/25`** 节点（`14/14` 带 ID）、`S02_preferences` **`24/24`** 节点全部 `hittable=True`（**0 误报**）；`R3` PASS（`1` tap，`0` dismiss） | **PASS** |
| **Check 1** | **Item 1**：前台激活门禁（无 `screen_id`） | 控制中心下拉遮挡 `S01_home`（`application_state=1`, `key_window_scene_activation_state=1`）；无 `screen_id` 的 `require_unique=True` 与传统 `require_unique=False + fallback_xy` 均阻断（`fail_kind="overlay_occluded"`，**`0` taps**） | **PASS** |
| **Check 2A** | **Item 2**：`screens.yaml` `blocking_overlays` 点击前硬拦截 | 部分 RN `<Modal>`（`"活动弹窗"`）下 `265 ms` 内阻断（`fail_kind="overlay_occluded"`），**`0` taps**，**`0` dismiss**，弹窗未被误关 | **PASS** |
| **Check 2B** | **Item 3**：`ax_dump-v9` `accessibilityViewIsModal` 遮挡检测（剥离 `blocking_overlays`） | `25/25` 底层首页节点准确标为 `hittable=False`（`occluded_reason="modal_sibling"`），`3/3` 顶层弹窗节点 `hittable=True`；`R3` 阻断（`fail_kind="locator_occluded"`，**`0` taps**，**`0` dismiss**） | **PASS** |
| **Check 2C** | **Item 4**：符号用例整案禁用自动关弹窗 | 符号用例（同时覆盖 `runner.run_case` 与 `run_recipe` 两条入口）在后续非符号 `assert_present` 失败时均 **`0` dismiss、`0` 误点**且退出后恢复上下文标志；非符号对照用例按既有策略自动关闭弹窗（`1` tap） | **PASS** |

---

## 四、Stage 3：`guest_axruntime` 进程外只读探针实测（STRONG GO）

按 [Guest 免侵入 AX 观测规格 §五](guest-ax-observation.md) 构建并验证了独立进程外只读探针，现已打包沉淀至公开核心路径：

- 源码：`support/guest-ax-probe/ax_runtime_probe.m`（仅链接 `Foundation` 与 `CoreGraphics`，零 `UIKit` 依赖与 RunLoop，通过 `dlopen` 加载系统私有框架）
- 最小权限声明：`support/guest-ax-probe/entitlements.plist`
- 编译签名脚本：`scripts/build_guest_ax_probe.sh`（产出 `arm64 + arm64e` 通用 Mach-O 可执行文件至 gitignored 路径 `support/guest_ax_probe`，使用 `codesign --generate-entitlement-der` 自签名，不改动 `vphoned`）

### 4.1 iOS 26.1 实测确认的运行时符号、属性号与最小 Entitlement

1. **实测已确认导出符号（`dyld_shared_cache` 动态核验）**：
   - `/usr/lib/libAccessibility.dylib`：`_AXSApplicationAccessibilityEnabled`、`_AXSApplicationAccessibilitySetEnabled`、`_AXSAutomationEnabled`、`_AXSSetAutomationEnabled`、`_AXSVoiceOverTouchEnabled`（仅只读校验恒为 `0`，**严禁调用 `_AXSVoiceOverTouchSetEnabled`**）。
   - `/System/Library/PrivateFrameworks/AXRuntime.framework/AXRuntime`：`AXUIElementCreateSystemWide`、**`_AXUIElementCreateAppElementWithPid`**（实测确认 `AXUIElementCreateApplication` 与 `_AXUIElementCreateWithPID` 在 iOS 26.1 **不存在**）、`AXUIElementSetMessagingTimeout`（`AXUIElementSetGlobalTimeout` 不存在）、`AXUIElementCopyMultipleAttributeValues`。
   - **关键属性号**：`-[AXElement axIdentifier]` 对应属性号 **`5019`**，返回真实 `accessibilityIdentifier` / React Native `testID`（而 `-[AXElement identifier]` / 属性 `2031` 仅返回内部数字 AX UID）。`visibleElements` 自动预取 `2001/2003/2004/2006/2031/2057` 六项核心属性，探针仅增量批量预取 `@[@2025, @5019, @12000]`（`isVisible`、`axIdentifier`、`respondsToUserInteraction`），将单帧访客侧延迟由全量重拉的 `99.1 ms` 降至 **`56.2 ms` P50**。
2. **最小 Entitlement 消融实测结论**：
   - 在目标 iOS 26.1 VM 上完成 9 组权限消融对比（空权限、`platform-application`、`com.apple.private.accessibility.inspection`、`com.apple.accessibility.api` 及多组合）；
   - **唯一必要且充分的最小权限为单一键值 `com.apple.accessibility.api = true`**，无需 `platform-application`、无需 `com.apple.private.accessibility.inspection`，也无需 `task_for_pid-allow`，即可完整支持冷唤醒（`0 -> 1` 且 VoiceOver 保持 `0`）、读取 `SpringBoard` `systemApplication`、`primaryApp`、跨进程坐标命中测试与 `--pid` 查询。

### 4.2 四项核心判据实测对比（`guest_axruntime` vs `frida_inproc` `ax_dump-v9`）

> 全部 `guest_axruntime` 实测（Phase A）严格在 `iproxy` 与 Frida 附加前完成，确保 `procexp` 内存残留测量无交叉污染。

| 评估判据（`guest-ax-observation.md` §1.2 & §5） | `guest_axruntime`（`guest_ax_probe`） | `frida_inproc`（`ax_dump-v9.dylib`） | 对比结论 |
| :--- | :--- | :--- | :--- |
| **(a) 目标控件覆盖率** | `S01_home` **`14/14` ID**（100%）；`S02_preferences` **`24/24` ID**（100%）；SwiftUI `Settings` **`10/10` 可点 ID**（100%）并准确反馈局部裁切 `visible_frame` | `S01_home` `14/14`；`S02_preferences` `24/24`；`Settings` 18 ID（10 可点 + 6 屏外 + 2 被底栏遮挡） | **PASS — 全部在屏可点控件 100% 一致** |
| **(b) 弹窗与跨进程遮挡识别** | **部分 RN `<Modal>`**：原生屏蔽全部 25 个被遮底层节点，仅暴露 3 个弹窗节点，跨进程命中测试直击 `"活动弹窗"`。**控制中心**：`is_control_center_visible=true`、`primary_app=SpringBoard`，**零注入暴露 27 个实时 SpringBoard 控件**，`applicationAtCoordinate` 返回 `SpringBoard` | **部分 RN `<Modal>`**：保留 28 节点（3 `hittable=true` + 25 `modal_sibling`）。**控制中心**：依赖 `app_state=1, scene_state=1` 拦截，但进程内 `hitTest:` 仍判 `hittable=true` 且 **0 SpringBoard 控件可见** | **PASS — 跨进程遮挡观测能力严格更强** |
| **(c) 连续可靠性与 `Tag: 255` 内存残留** | **0 / 50 失败（`0.0%`）**；VoiceOver 恒为 `0`；`FoodAIVerification`（连续 45+ 次后 **`0 / 234`**）、`SpringBoard`（**`0 / 1,061`**）、`Preferences`（**`0 / 75`**）全部 **`0` 个 `Tag: 255` 区域** | `S01_home` `0 / 20` 失败；Frida 卸载后目标 App 进程永久残留 **`11` 个 `Tag: 255` 区域（~19.1 MB）**；`SpringBoard` 因禁止 Frida 附加才保持为 `0` | **PASS — 零注入、零 `Tag: 255` 脏页、0% 失败率** |
| **(d) 单次快照延迟（`S01_home`，N=20）** | **冷附加：`0 ms`**（免 Frida/`iproxy`）；访客侧 P50/P95 **`56.23 / 59.24 ms`**；宿主端到端 P50/P95 **`66.50 / 69.95 ms`**（`--system-wide --hit-point`：`81.67 / 91.74 ms`） | **冷附加：`569.15 ms`**；默认 `settle_s=0.8` 宿主 P50/P95 **`811.68 / 816.69 ms`**；极限 `settle_s=0.05` 宿主 P50/P95 **`59.97 / 63.36 ms`** | **PASS — 较默认 `AxSession` 快 12.2 倍，且免秒级冷附加与重启清理** |

### 4.3 Go/No-Go 结论与接入约束

- **综合裁决：STRONG GO** — 建议按 [Guest 免侵入 AX 观测规格 §四](guest-ax-observation.md) 将 `guest_axruntime` 接入 `AxSession` 作为显式可选后端（`capabilities.ax.backend: guest_axruntime`），在完成多应用回归长跑前保留 `frida_inproc` 作为默认基线及 `api_capture` / 灰盒观测通道。
- **实测发现的两条强约束（接入 `AxSession` 时必须落实）**：
  1. **显式 `--pid` 必须叠加系统前台门禁**：控制中心遮挡时，直接调用 `_AXUIElementCreateAppElementWithPid(<app_pid>)` 仍可读到后台 App 的 23 个节点，同时 `system.is_control_center_visible == true` 且 `system.primary_app.bundle_id == "com.apple.springboard"`。因此 `guest_axruntime` 适配器必须在前台非目标 App、控制中心展开或锁屏时强制 fail-closed 报 `overlay_occluded`。
  2. **保持 `ax_backend` 证据与失败分类隔离**：在部分弹窗遮挡下，`guest_axruntime` 的 `visibleElements` 会直接省略被遮底层节点（表现为缺失而非 `modal_sibling` 标记），且控制中心下 `primaryApp == systemApplication` 时需避免 `--system-wide` 重复序列化；不同后端的 `unique_visibility` 证据继续严格隔离不混用。

### 4.4 Stage 3B 正式接入与 `vm-2607` 真机全链路实测（2026-11-27）

独立探针已作为受控资产落盘至 `support/guest-ax-probe/` 与 `scripts/build_guest_ax_probe.sh`（通用二进制 `arm64 + arm64e`、仅链接 `Foundation`/`CoreGraphics`、单条 `com.apple.accessibility.api = true` entitlement），并以显式 opt-in 方式接入 `AxSession(..., ax_backend="guest_axruntime")`（`frida_inproc` 保持为默认基线与灰盒通道），在 `vm-2607` 上完成真机端到端闭环验证：

- **自动部署与版本/几何对齐（E2E-1）**：首次 `AxSession.open()` 自动推送 `/var/jb/tmp/autophone-guest-ax-probe-<sha16>`（`pushed`），同进程复用命中缓存（`cached`）；成功采集 `com.autophone.foodorderingdemo.v2` `1.0.0`/`1` `S01_home`（20 节点、`screen={scale: 3.0, w: 430.0, h: 932.0}`、`snapshot_kind="ui_structure"`），连续 10 次 `AxSession.refresh()` 端到端 P50/P95 延迟 **`289.84 / 416.67 ms`**（全程无 Frida attach/detach）。`ax_checkpoint.capture()` 落盘通过 `operation-ax-checkpoint.v2.json` 校验；`variant_evidence.classify()` 对 `ax_ui_text` 放行，对 `core_location_authorization` 强制拒止（`variant_inproc_graybox_required`），`ax_comparison.compare()` 混用后端时强制拒止（`ax_backend_mismatch`）。
- **实机 `tap_ax` 与多应用覆盖（E2E-2）**：`CaseContext.tap_ax(identifier="home.ai_prompt.preferences", role="button", require_unique=True, screen_id="S01_home")` 仅下发 `1` 次 UDS tap 即完成 `S01_home -> S02_preferences`（24 节点含「饮食偏好」「保存偏好」）导航并可正常返回首页；系统应用 `com.apple.Preferences` 同步通过 `foreground_active=True` 快照采集（13 节点）。
- **强制 `--pid` 前台门禁与挂起进程保护（E2E-3）**：真机实测发现按 Home 键回到桌面后 iOS 会挂起后台 App 进程，若探针仍向 `--pid` 发起同步 AX Mach IPC 会阻塞至 shell 超时；已在 `ax_runtime_probe.m` 修复为在调用 `_AXUIElementCreateAppElementWithPid` 前先行核验 `systemApplication` 的锁屏/控制中心/`primaryApp.pid` 归属并立即短路退出。实测在 **Home 桌面**（`foreground_app_mismatch:com.apple.springboard`，`165.56 ms`）与 **控制中心遮挡**（`control_center_visible`，`149.90 ms`）两种场景下，无论 `tap_ax(..., fallback_xy=...)`（不带 `screen_id`）还是 `tap_ax(..., require_unique=True)` 均严格 fail-closed 报错 `overlay_occluded`、发出 **`0` 次** UDS tap，且 `refresh()` 返回 `foreground_active=False`、`nodes=[]`、`snapshot()` 抛出 `AxUnavailable`。
- **零侵入洁净度与 VoiceOver 红线（E2E-4）**：全程结束后 `inspect_app_residue`（`158` 内存区域）与 `inspect_springboard_residue`（`1045` 内存区域）均判定 `clean`（`Tag: 255` 严格为 `0`），`_AXSVoiceOverTouchEnabled()` 严格保持 `false`（`0`）。

---

## 五、Stage 2 真机全链路联合裁决实测（9/9 全绿）

### 5.1 实验基线、双后端共存配置与制品身份（`vm-2607`，2026-11-27）

- **被测验证制品**：基于 `Food Ordering`（`com.autophone.foodorderingdemo.v2` `1.0.0 (1)`）构建带真实 `NSURLSession` 请求的 Stage 2 验证包，经隔离 Package Store 按 `--build-id` 完成安装验真（`verified=true`、`release_like=true`；安装前 SpringBoard 内存巡检 `verdict="clean"`、`1,065` 区域、`Tag: 255 == 0`）：
  - `ipa_sha256`：`a8a6a48b282e7a073fd6ca3ac71e623b42662dccfaa5c02e727feeca32c73fd8`（前 12 位 `a8a6a48b282e`）
  - `source_tree_sha256`：`099b7818da72e3b856ebccff5947045456c6dba2326c3a097e2c62dc57e727b6`
  - `build_id`：`bld-88aac3fa4623ae14f12ff087bb483b42e155acc7d7f29f05da6fab5d1ac8bb6f`
  - `binding_sha256`：`437b9d1097f8e6752df6c98dcfc39c03b6d89709ae0acba352c129f6b2921d35`
- **全链路运行配置（9/9 共用）**：在 `vm-2607`（iOS `26.1 / 23B85`）上走完整 `RunService` / `RunExecution` 链路；UI 观测采用 `ax_backend="guest_axruntime"`（`support/guest_ax_probe`，零 AX Frida 附加），API 抓取采用 `capture_mode="attach"`（真实 Frida `net_tap.js` 捕获宿主机本地受控 HTTP stub 流量），同步实证了 **`guest_axruntime` 与 Frida `api_capture` 共存且无双 Frida 附加冲突**；`package.build_identity_match="match"`（`comparison_granularity="build_marker"`）。

### 5.2 S2-1–S2-7 真机联合裁决实测矩阵（9/9 PASS）

| 场景（CE 编号） | Run ID | Run 状态 | 三平面（`ui / api / readback`） | `joint_verdict`（`status` / `fail_kind` / 关键 `mismatch_reasons`） | `capture.jsonl`、`entity-readback.json` 与 `negctl` 核心实证 |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **S2-1** 正向（`R5`） | `s2-1-pos-20261127-160927-food_ordering-c2f7` | `passed` | `pass / pass / pass` | `pass` / `""` / `[]`（`store-flagship-a`） | `42` 事件；`GET /api/v1/preferences?store_id=store-flagship-a` `200`（`step=tap_ax`、`symbol_id=ctrl.home.preferences`、`window_tag=symbol:ctrl.home.preferences`）；readback `pass`（3/3 `ok`，`record_sha256=sha256:99c6b4fb73dd...`） |
| **S2-2** 错实体（CE #1，`R5`） | `s2-2-wrong-20261127-161008-food_ordering-1991` | `failed` | `pass / fail / fail` | `fail` / `entity_api_mismatch` / `entity_api_field_mismatch:resp.data.{store_id,store_name,preference_profile}`, `ui_passed_while_entity_api_mismatched`, `entity_readback_mismatch`, `ui_passed_while_readback_mismatched` | `41` 事件；API `200` 返回错实体 `store-other-b` / `分店-B` / `正常冰全糖`；readback `fail`（`entity_readback_expect_mismatch`，0/3 `ok`，`record_sha256=sha256:7a28f821e4d5...`） |
| **S2-3** 乐观 UI + HTTP 500（CE #2，`R5`） | `s2-3-opt500-20261127-161047-food_ordering-81dc` | `failed` | `pass / fail / pass` | `fail` / `optimistic_ui_api_failed` / `api_http_status_500`, `api_status_mismatch`, `entity_api_field_mismatch:resp.data.*`, `optimistic_ui_api_failed` | `40` 事件；API 返回 `500`（`INTERNAL_BACKEND_ERROR`）而 UI 乐观渲染目标文案；readback `pass`（`sha256:99c6b4fb73dd...`），联合裁决 fail-closed 拦截 |
| **S2-4** 陈旧回读（CE #3，`R5`） | `s2-4-stale-rb-20261127-161125-food_ordering-5969` | `failed` | `pass / pass / fail` | `fail` / `entity_readback_mismatch` / `entity_readback_mismatch`, `ui_passed_while_readback_mismatched` | `40` 事件；API `200` 响应全匹配，但动作后回读返回陈旧数据（`preference_profile`：期望 `少冰少糖-VIP`，实得 `陈旧偏好-STALE`；`record_sha256=sha256:3f9286306927...`） |
| **S2-5a** 缺失 API `mode=disabled`（CE #4a，`R5`） | `s2-5a-noapi-20261127-161203-food_ordering-91d4` | `failed` | `pass / missing / pass` | `fail` / `api_evidence_missing` / `api_evidence_missing` | `capture.jsonl` 缺失；readback `pass`；严格 fail-closed，绝不降级为 UI-only 通过 |
| **S2-5b** 缺失抓取会话（CE #4b，`R5`） | `s2-5b-nocap-20261127-161220-food_ordering-b3d5` | `failed` | `pass / missing / pass` | `fail` / `api_evidence_missing` / `api_evidence_missing` | `planes.api.mode=required` 且 `capture_session_factory=None`；`capture.jsonl` 缺失；严格 fail-closed |
| **S2-6** 动作窗口隔离（CE #5，`R5_ISO`） | `s2-6-winiso-20261127-161236-food_ordering-f449` | `failed` | `pass / fail / pass` | `fail` / `optimistic_ui_api_failed` / `api_expected_call_missing`, `optimistic_ui_api_failed` | `43` 事件；真实 `NSURLSession` `200` 发生于点击前置操作内（`operation=trigger_home_pref_before_tap`、`symbol_id=null`、`window_tag=null`），`symbol:ctrl.home.preferences` 窗口内命中 `0` 次 -> 严格拒止 |
| **S2-7a** 强联合负对照（CE #6a，`neg.r5.joint_strong`） | pos `s2-7a-pos-...-5153`；neg `s2-7a-neg-...-2d4f` | pos `passed`；neg `failed` | neg: `pass / fail / pass` | neg: `fail` / `optimistic_ui_api_failed` / `api_expected_call_missing`（`failed_step_index=null`） | `skip_step tap_ax` 后共享文案 `"微辣"` 保持 UI 为绿，联合裁决在 API 平面翻转：`target_green=true`、`negative_verdict=failed_on_expected_step`、`discriminator_flipped=true`、`teeth_at=joint`、`counted=true`、`discrimination_gaps=[]` |
| **S2-7b** 弱/无牙负对照（CE #6b，`neg.r5.joint_weak`） | pos `s2-7b-pos-...-0cf8`；neg `s2-7b-neg-...-8b8f` | pos `passed`；neg `passed` | neg: `pass / skipped / skipped` | neg: `pass` / `""` / `[]`（`require_api=false, require_readback=false`） | `skip_step tap_ax` 后弱策略保持全绿：`target_green=true`、`negative_verdict=passed`、`discriminator_flipped=false`、`teeth_at=none`、`counted=false`、`discrimination_gaps=["discrimination_toothless"]` |

### 5.3 原制品还原、洁净度核验与真机发现修复

- **原制品逐字节还原与 SpringBoard 洁净**：实验结束后将基线 `Food Ordering` IPA（`a95a2817444a...`）重新安装至 `vm-2607`，校验 Guest 内 `main.jsbundle` SHA-256 精确恢复为实验前基线 `dffbb57b5eab3effc60b06386f3b578fa38b5382f5bd87a4aea7b15b5cbf37ef`；事后 SpringBoard `procexp` 复检 `verdict="clean"`（`1,090` 区域、`0` 个 `Tag: 255` 脏页、`0` 个残留 agent 区域）。
- **真机发现与闭环修复（`build_marker_not_compared` -> 提交 `10b46713f`）**：实测发现虽然 `world-identity.json` 已通过包内 `autophone-ui-eval-build-marker.json` 确认构建一致（`package.build_identity_match="match"`），但 v2 Package Store 下的 `build-identity.json` 仍停留在 `identity_state="ambiguous"`（`match.reason="build_marker_not_compared"`），导致 `run_execution.py` 在严格 D2 门禁下跳过回写 `symbol.unique_visibility` claim。随后在提交 `10b46713f` 中补齐基于实时 `file_get` 逐字节比对 `autophone-ui-eval-build-marker.json` 的 v2 构建身份确认链路（仅当版本元组、构建标记字节均精确匹配且 `source_revision` 非 `unknown` 时晋升为 `identity_state="confirmed"`，标记冲突判 `mismatch`、缺失/未比对维持 `ambiguous`），并在 §六 Stage 3B 多应用长跑中完成真机验证。

---

## 六、Stage 3B 多应用零 Frida 20 轮长跑与默认后端晋升评估（20/20 全绿）

### 6.1 零 Frida 运行期加固与测试配置（提交 `46b8870a2` + `10b46713f`）

为评估 `guest_axruntime` 作为纯 UI 回归后端的长期稳定性，先落地零 Frida 执行链加固（提交 `46b8870a2`：无 API/Frida 需求时 `need_frida=False`、`crash_trap` 默认关闭、`app_frida_hygiene` 无 Frida 附加且预检洁净时跳过 `app_terminate` 并记录 `post_run.reason="no_frida_attached"`、`operation_run.py` 为 `requires={"ax"}` / `tap_ax` 供应惰性 `AxSession`）与前述 v2 构建标记确认修复（`10b46713f`）。随后在 `vm-2607` 上于**纯零 Frida 环境**（不启用 `--group frida`、`AUTOPHONE_CRASH_TRAP` 与 `AUTOPHONE_FRIDA_HOST` 均未设置、`iproxy` 停止且本地 `27042` 端口关闭）完成四轨验证与 20 轮双应用交替长跑。

### 6.2 分轨真机验证摘要（Pre-Soak / Tracks 1–4 / Post-Soak）

| 验证轨 | 范围 / 关键 Run ID | 结果 | 核心真机实证 |
| :--- | :--- | :---: | :--- |
| **Pre-Soak 洁净基线** | `SpringBoard`、`FoodAIVerification`、`Preferences` | **PASS** | 实机 `procexp` `Tag: 255` 依次为 `0 / 1,049`、`0 / 211`、`0 / 394`；`voiceover=false`；基线 `main.jsbundle`（`dffbb57b...`）完整；本地 Frida `27042` 端口关闭 |
| **Track 1**（`Food Ordering` `R3`，纯 `guest_axruntime` + API 禁用） | `p2b-t1-r3-20261127-170249-food_ordering-1167`（基线 IPA `a95a2817444a...`，`build_id=bld-d00d0820...3026`） | **PASS** | `build-identity.json` 确认 `identity_state="confirmed"`（`match_basis="installed_build_ref_build_marker"`、`matched_registry_sha256=a95a2817...c6e104`）；`symbols/claims.json` 追加 `clm-sym-34b422b255cb`（`symbol.unique_visibility`、`vm_observation`、`ax_backend="guest_axruntime"`），`has_reusable_unique_visibility("guest_axruntime")=True`、`frida_inproc=False`，`binding_sha256`（`d037a28fbfaa...`）与 YAML 逐字节不变；`app-frida-hygiene.json` 记录 `post_run={result:"skipped", reason:"no_frida_attached"}`；`procexp` `FoodAIVerification` `0 / 227`、`SpringBoard` `0 / 1,082` `Tag: 255`；`voiceover=false` |
| **Track 2**（`ios-settings-smoke` `R1`/`R2`/`R3_AX`，纯 `guest_axruntime`） | `R1` `p2b-t2-r1-...-4b80`；`R2` `p2b-t2-r2-...-1863`；`R3_AX` `p2b-t2-r3ax-...-8936` | **PASS** | 直接 `AxSession.snapshot()` 成功回读 `short_version="1"`、`bundle_version="1353.1.11.100"`、`foreground_active=true`、`nodes=13`、`snapshot_kind="ui_structure"`、`voiceover=false`（自动对齐简体中文 `com.apple.settings.general` -> `关于本机`）；`R1=passed`、`R2=failed`（负向哨兵）、`R3_AX=passed`（`tap_ax`），3 场均 `no_frida_attached=true`；`procexp` `Preferences` `0 / 325`、`SpringBoard` `0 / 1,058` `Tag: 255` |
| **Track 3**（与 `api_capture` 共存） | 复用 §五 Stage 2 9/9 真机证据 | **PASS** | 9/9 全绿证明 `guest_axruntime` 与 Frida `net_tap.js`（`mode=attach`）共存零冲突；本阶段 Tracks 1/2/4 在完全关闭 `iproxy` 下执行 |
| **Track 4**（20 轮双应用交替长跑：10× `Food Ordering R3` ↔ 10× `ios-settings-smoke R3_AX`） | `p2b-s01f-...` .. `p2b-s10s-...`（20 次连续 `RunService` 全链路运行） | **PASS（20/20）** | 通过率 **`20/20`（`100.0%`）**；`no_frida_attached` **`20/20`**；`Food Ordering` `identity_state="confirmed"` **`10/10`**，`symbol.unique_visibility` 累计 11 条且 `binding_sha256` 与 YAML 不变；`AxSession.refresh()` **`40/40` 成功（`0.0%` 失败率）**、`first_wake_retries=0`（`max=0`）、`voiceover=false`（长跑 `40/40`，全阶段合计 **`46/46`**） |
| **Post-Soak 洁净复检** | `SpringBoard`、`FoodAIVerification`、`Preferences` | **PASS** | 实机 `procexp` `Tag: 255` 严格为 `0 / 1,099`（`SpringBoard`）、`0 / 210` 与 `0 / 227`（`FoodAIVerification`）、`0 / 306`（`Preferences`）；`voiceover=false`；基线 `main.jsbundle`（`dffbb57b...`）完整未改 |

### 6.3 20 轮交替长跑延迟与可靠性遥测（Track 4，每轮 2 次 `refresh()`）

> 每轮运行触发 2 次 `guest_axruntime` `AxSession.refresh()`：1 次用于 `RigMaintenance` 就绪锚点探针，1 次用于 `tap_ax`。

| 统计切片 | `refresh()` 调用 / 失败率 | `AxSession.refresh()` 端到端耗时（ms）：Min / P50 / P95 / Max / Mean | 访客探针 `elapsed_ms`（ms）：Min / P50 / P95 / Max / Mean | `first_wake_retries` / `voiceover` |
| :--- | :---: | :---: | :---: | :---: |
| **长跑总计（20 runs）** | `40` / `0`（`0.0%`） | `218.44` / **`267.14`** / **`444.31`** / `646.76` / `290.14` | `96.03` / **`127.67`** / **`267.50`** / `329.46` / `146.82` | `0`（`max=0`） / `false`（`40/40`） |
| **`Food Ordering`（`R3`，10 runs）** | `20` / `0`（`0.0%`） | `218.44` / **`245.69`** / **`284.55`** / `308.74` / `250.37` | `96.03` / **`106.58`** / **`144.56`** / `149.21` / `110.26` | `0`（`max=0`） / `false`（`20/20`） |
| **`iOS Settings`（`R3_AX`，10 runs）** | `20` / `0`（`0.0%`） | `250.04` / **`288.55`** / **`478.90`** / `646.76` / `329.92` | `118.16` / **`160.65`** / **`317.66`** / `329.46` / `183.38` | `0`（`max=0`） / `false`（`20/20`） |
| **Phase 2B 全轨合并** | `43` refresh + `1` snapshot + `2` vo（`46/46` OK） | `218.44` / **`261.08`** / **`467.36`** / `970.19` / `304.02` | `96.03` / **`136.44`** / **`304.00`** / `333.50` / `151.52` | `0`（`max=0`） / `false`（`46/46`） |

### 6.4 默认后端晋升评估与实测运维前置条件

- **核心后端裁决 — 生产级 opt-in 就绪（STRONG GO）**：`guest_axruntime` 通过全部定量验收门禁（`20/20` 多应用交替长跑、`46/46` 探针零失败、`267.14 ms` P50 端到端 `refresh()`、双目标应用与 `SpringBoard` 全部 `0` 个 `Tag: 255` 脏页、`0` VoiceOver、零 Frida 运行时依赖、v2 `build_marker` 确认构建身份并安全回写隔离的 `symbol.unique_visibility` claim，以及与 `api_capture` 9/9 共存通过）。
- **全局切换 `DEFAULT_AX_BACKEND` 前需补齐的两项运维前提（长跑 Pass #1 实测发现）**：
  1. **零 Frida 跨 Run 进程驻留复位缺口（`autophone/rig/maintenance.py`）**：在 `frida_inproc` 基线下，`app_frida_hygiene` 于 Run 结束时总会终止 App，使下一轮自然冷启动回到首页；切换为零 Frida（`post_run.reason="no_frida_attached"`）后，被测 App 进程保留在上一轮的二级子页（如 `S02_preferences` / `通用`）。当前 `RigMaintenance._reset_home_and_probe()` 仅派发显式注册的 `reset_to_home` Operation，即使 `profile.yaml` 声明了 `reset: relaunch` 也不会自动回退调用 `relaunch_app()`。
  2. **就绪锚点包含子页共有文案（`examples/ios-settings-smoke/apps/ios_settings_smoke/profile.yaml`）**：`ready_markers` 当前包含同时出现在二级页标题栏的 `"General"` / `"通用"`，掩盖了未回到首页的状态漂移。在长跑覆盖配置中将 `ready_markers` 收紧为首页独有锚点（`["今晚", "立即下单"]` / `["飞行模式", "Airplane Mode", "Wi-Fi"]`）并注册 `reset_to_home`（`relaunch_app`）后，交替长跑通过率即由子页漂移导致的 `0/20` 提升至 **`20/20`（`100%`）**（另记：由 `Settings` 切回 `Food Ordering` 时，iOS 左上角 `《设置` 返回提示条会使顶栏 `"饮食偏好"` 的 Vision OCR 置信度降至 `0.3` `< 0.5`，而 `S02_preferences` 主体独有锚点 `["辣度", "恢复默认", "保存偏好"]` 置信度均为 `1.0`）。
- **晋升裁决与正式落地（提交 `3ed53ebf6` + `86825c4a7`）**：上述两项全局默认切换前置项（`RigMaintenance._reset_home_and_probe()` 的 `profile.reset == "relaunch"` 自动回退与 `examples/ios-settings-smoke` 根页独有 `ready_markers` 收紧）已于提交 `3ed53ebf6` 落地并通过 12 项回归单测验证（`tests/test_rig_maintenance_reset.py`）；随后 `DEFAULT_AX_BACKEND` 与 `app-profile.v1.json` 已于提交 `86825c4a7` 正式晋升为 **`"guest_axruntime"`**（纯 UI 场景默认零 Frida），同时完整保留 `frida_inproc` 作为显式可选后端（`capabilities.ax.backend: frida_inproc`）支撑 `api_capture`、`core_location_authorization`、`modal_sibling` 等进程内灰盒诊断。

---

## 七、`upstream_v1` + iOS `27.0.1`（`24A446`）真机实测验证（2026-11-27）

为核验 `DEFAULT_AX_BACKEND = "guest_axruntime"` 晋升后在上游 `upstream_v1` 协议与最新 iOS `27.0.1`（构建号 `24A446`）客机镜像上的兼容性，在无 Frida 依赖的虚拟环境（未安装 `frida`、`AUTOPHONE_FRIDA_HOST` 与 `AUTOPHONE_CRASH_TRAP` 均未设置、无 `iproxy` 隧道）中对 schema v2 `upstream_v1` `vphone-cli` 虚拟机（4 vCPU / 4 GiB，客机无 `procexp`，使用 `scripts/build_guest_helpers.sh` 编译的 `autophone-springboard-regions` 做 SpringBoard 只读区域巡检）完成端到端真机闭环验证：

### 7.1 探针构建、自动部署与核心遥测（iOS `27.0.1` / `24A446`）

- **探针产物与自动部署**：经 `./scripts/build_guest_ax_probe.sh` 编译 `arm64 + arm64e` 通用二进制（Xcode `iPhoneOS26.4.sdk`，单条最小权限 `com.apple.accessibility.api = true`，`binary_sha256` 前 16 位 `07c26d49b1a145f1`），经 `upstream_v1` `file_put` + SHA-256 核验首次推送至 `/var/jb/tmp/autophone-guest-ax-probe-07c26d49b1a145f1`（`action="pushed"`），同进程二次调用命中缓存（`action="cached"`）。
- **`AXRuntime` 导出符号、属性 `5019`（`axIdentifier`）与几何/版本回读**：
  - 系统 SwiftUI `Settings`（`com.apple.Preferences`，`en_US`）：`_AXUIElementCreateAppElementWithPid`、`AXUIElementCreateSystemWide`、`AXUIElementSetMessagingTimeout` 与属性 `5019` 全部可用；直采 `14` 个可见节点中 **`12/14`** 携带真实 `axIdentifier`（覆盖全部 12 个交互按钮：`com.apple.settings.primaryAppleAccount`、`airplaneMode`、`wifi`、`bluetooth`、`battery`、`general`、`accessibility`、`actionButton`、`appearance`、`camera`、`controlCenter`、`Dictate`），完整回读 `short_version="1.0"`、`bundle_version="2027.0.10.401"`；`screen={scale: 3.0, w: 430.0, h: 932.0}` 与 UDS `capabilities.screen={width: 1290, height: 2796, scale: 3}` 精确对齐。
  - 默认 `AxSession("com.apple.Preferences")`（不传 `ax_backend`，自动解析 `guest_axruntime`）：`snapshot()` 耗时 `408.71 ms`（`snapshot_kind="ui_structure"`、`foreground_active=true`、`14` 节点）；连续 `10×` `AxSession.refresh()` **`10/10` 成功**（`14` 节点稳定、`first_wake_retries=0`），宿主端到端耗时 Min / P50 / P95 / Max / Mean = `295.30` / **`369.80`** / **`555.59`** / `581.60` / `396.35 ms`；连续 `10×` 直调探针 **`10/10` 成功**，访客侧 `elapsed_ms` P50/P95 **`102.11 / 131.38 ms`**（宿主端到端 P50/P95 **`126.06 / 158.52 ms`**），`voiceover=false`（`10/10`）。

### 7.2 跨进程前台门禁、零 Frida 回归与真机发现修复

| 验证项 | 场景与配置 | iOS `27.0.1`（`24A446`）`upstream_v1` 真机实测结果 | 状态 |
| :--- | :--- | :--- | :---: |
| **系统弹窗实时拦截（实机突发）** | 冷启动 USB 配对触发 SpringBoard `"Trust This Computer?"` 系统弹窗覆盖 `Settings` | 尽管 `RunningBoard` `app_foreground` 仍报 `com.apple.Preferences`（`verified=true`），`guest_axruntime` 零注入完整读出 4 个 SpringBoard 弹窗节点（含属性 `5019` `com.apple.lockdown.ax.trust.deny_trust` / `confirm_trust`），并严格 fail-closed（`foreground_active=false`、`foreground_occlusion_reason="foreground_app_mismatch:com.apple.springboard"`、`nodes=[]`、`snapshot()` 抛 `AxUnavailable`） | **PASS** |
| **Home 桌面门禁** | `Settings` 退回主屏后调用 `refresh()` / `snapshot()` / `tap_ax` | `refresh()`（`245.88 ms`）返回 `foreground_active=false`、`foreground_occlusion_reason="foreground_app_mismatch:com.apple.springboard"`、`nodes=[]`；`snapshot()` 抛 `AxUnavailable`；`tap_ax` fail-closed 报 `overlay_occluded`（`0` taps）；直采 SpringBoard 暴露 `23` 节点（`voiceover=false`） | **PASS** |
| **控制中心门禁** | 下拉控制中心遮挡 `Settings` | `refresh()`（`249.10 ms`）返回 `is_control_center_visible=true`、`foreground_active=false`、`foreground_occlusion_reason="control_center_visible"`、`nodes=[]`；`tap_ax` fail-closed 报 `overlay_occluded`（`0` taps）；直采暴露 `31` 个实时 SpringBoard 控件（`voiceover=false`） | **PASS** |
| **锁屏门禁 + 实机发现修复** | 按 `power` 锁屏后调用 `refresh()` / `tap_ax` | 首次实测发现 iOS `27.0.1` 锁屏熄屏态下 SpringBoard `frame` 上报为按 scale 二次缩放值（`143.33×310.67`，即 `430/3×932/3`），触发误报 `screen geometry mismatch`。已在 `autophone/device/guest_ax_probe.py`（`_resolve_screen_geometry`）修复为在 `foreground_active=false` 且具备 UDS `capabilities.screen` 时保留遮挡/锁屏归因（`nodes=[]` 不做坐标映射，前台活跃态仍保持严格几何校验），并补充单测（`tests/test_stage3_guest_axruntime.py` `39/39` PASS）。复测 `refresh()`（`260.44 ms`）准确返回 `is_screen_locked=true`、`foreground_occlusion_reason="screen_locked"`、`nodes=[]`，`tap_ax` 报 `overlay_occluded`（`0` taps） | **PASS** |
| **默认零 Frida 回归（`ios-settings-smoke`）** | 完整 `RunService` 零 Frida 执行 `R1`、`R2` 与 2 轮 `R3_AX`（`tap_ax(identifier="com.apple.settings.general")` -> `assert_present(["About"])`，未显式指定 `ax_backend`） | `R1` **PASS**、`R2` 确定性 **FAIL**（`assertion_failed`，负向哨兵）、`R3_AX` 连续 **`2/2` PASS**（验证 `reset: relaunch` 在两轮间复位至根页）；4 场均采用默认 `ax_backend="guest_axruntime"`、`exec_via="recipe"`，冷启轮次 `app-frida-hygiene.json` 记录 `post_run={result: "skipped", reason: "no_frida_attached"}`（注：`upstream_v1` 客机无 `procexp` 且 `autophone-springboard-regions` 按安全边界仅接受 `SpringBoard` PID，故已运行目标 App 的预检维持 fail-open 语义，而 `SpringBoard` 巡检严格闭环） | **PASS** |
| **React Native `Food Ordering` 零 Frida 实测** | 客机预装 `com.autophone.foodorderingdemo.v2`（`1.0.0 / 1`）默认 `guest_axruntime` 快照与 `tap_ax` 导航 | `AxSession.snapshot()`（`341.28 ms`）完整回读 `1.0.0 / 1`、`screen={scale: 3.0, w: 430.0, h: 932.0}`、`21` 节点（**`13`** 个携带属性 `5019` `axIdentifier`）；`tap_ax(identifier="home.navigation.orders", require_unique=True)` 零 Frida 导航至空购物车页（`screen_changed=true`、`4/4` 节点带 `axIdentifier`：`cart.header.title` / `cart.empty.icon` / `cart.empty.message` / `cart.empty.primary_action`） | **PASS** |
| **洁净度与 VoiceOver 红线** | `autophone-springboard-regions` 前后巡检 + 终态 `_AXSVoiceOverTouchEnabled` | `SpringBoard` 试验前 `verdict="clean"`（`910` 区域、**`0` 个 `Tag: 255`**），试验后 `verdict="clean"`（`1,027` 区域、**`0` 个 `Tag: 255`**）；全过程（冷启、直采、门禁、回归及终态）`_AXSVoiceOverTouchEnabled` 严格保持 **`false`（`0`）** | **PASS** |
