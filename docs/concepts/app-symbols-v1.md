# 统一 App 符号头文件（`app-symbols.v1`）最小契约规格

> **文档状态**：公共符号层最小可验证规格（RFC v0.3，已完成 Round 1 / Round 1.5 / Stage 2 / Stage 3 全阶段代码落地与真机验证；当前默认 AX 后端已由 Round 1 的 `frida_inproc` 晋升为 `guest_axruntime`，`frida_inproc` 保留为显式可选灰盒后端）。全局背景见 [iOS App 受控操作与确定性验证架构蓝图](app-operation-language.md)；分阶段任务与落地台账见 [受控操作与符号绑定分阶段实施任务书](app-symbols-v1-plan.md)；真机实证汇总见 [实测验证报告](app-symbols-v1-validation-report.md)；现有资产与运行期硬约束见 [编写用例与 Recipe](../guides/authoring-cases.md)、[回归运行期契约](runtime-contract.md) 与 [核心 JSON Schema 字典](../reference/schemas.md)。

---

## 一、设计目标与首版范围边界

### 1.1 核心验收目标
第一阶段直接依托现有 `Case`、`Recipe`、`Operation`、`screens.yaml` 与 `autophone/assets/promotion.py` 转正流程，集中解决确定性回归最核心的三件事：

1. **确认跑对了包**：精确绑定不可变制品内容摘要，杜绝同版本号异包混淆，并将精确制品摘要与符号绑定摘要校验全局延伸至负对照与转正证据复用；
2. **确认点对了控件**：加载期固定定位规则，点击期核验 `screens.yaml` 页面锚点与区域内唯一命中，零坐标回退且零静默绕过；
3. **确认产品出错时测试会失败**：定位、产品预期与资产转正分别取证，缺乏区分力的弱断言必须被负对照拦截在正式回归之外。

### 1.2 首版范围与明确延后项
- **首版必选范围**：
  1. **不可变符号绑定文件与可变关系证据物理分离**：`app-symbols.v1` 仅承载 `artifact_identity + controls`（原始文件字节即 `binding_sha256`），关系级证据独立存储于外部 claims 文件（复用 `knowledge-claims.v1.json` 结构与枚举）；
  2. **复用 `tap_ax` 的受控符号寻址**：扩展 `tap_ax` 支持 `screen_id`（复用 `screens.yaml` 单帧前置核验）与 `require_unique`，`Recipe` 通过 `action: tap_ax` + `symbol: <id>` 引用并由统一加载期绑定器解析冻结；
  3. **多副作用集合与机器可验的试跑隔离门禁**：`verbs` + `completeness`（`exhaustive | lower_bound | unknown`），配合 CLI 显式授权与 `profile.yaml` 数据隔离标记；
  4. **L2 薄清单导入与 L1 最小 RN 提取对照**：支持构建清单导入与未加密 IPA `main.jsbundle` 候选提取，跑通双分母量化评估。
- **延后或明确未纳入本规格的议题**：
  - **多步 `Operation` 参数槽位（`$param`）**、**路由表（`routes`）**、**数据源映射（`data_sources`）**、**分层组合 `Screen`**、**原生 Mach-O 静态反编译提取**与 **Agent 第二运行模式**：按真实痛点延后立项；
  - **业务对象身份与后端 API / 数据模型联合判定契约（见 §2.5）**：作为阶段二独立专项设计，首版不靠 UI 符号层越界承诺。

---

## 二、五项核心架构与实施约束

### 2.1 精确绑定制品内容、物理拆离可变观测，并全局收紧转正新鲜度

现有代码中存在两处容易导致跨包错配的边界：
- `autophone/assets/build_identity.py` 的 `subject_build_key` 仅按 `(bundle_id, short_version, build_version)` 计算摘要；`autophone/assets/promotion.py` 的 `discrimination_gaps()` 仅比对 `subject_build_key` 或 `build_label`，未读取负对照探针中的 `build_id` / `artifact_sha256`，导致同版本号异包不会触发 `discrimination_stale`。
- `autophone/packages/store.py` 的 `install_target_fingerprint()` 按 `(device, vm, vm_dir, socket)` 生成目标 Rig 指纹（`build-identity.v2.json` 的 `candidate_identity` 亦内嵌该字段）。**同一台 Rig 更换安装包后，`target_fingerprint` 仍然相同**，绝不能用作符号文件归档键或跨包证据作用域主键。

首版执行以下五条硬规则：
1. **三层身份与文件物理分离**：
   - **逻辑控件身份（`symbol_id`）**：如 `ctrl.home.preferences`，跨版本稳定复用，供 `Operation` 与 `Recipe` 引用；
   - **不可变制品符号绑定文件（`app-symbols.v1`）**：仅包含 `artifact_identity` 与 `controls`，**不含任何运行期观测或可变 claims**。原生 IPA 绑定 `package-manifest.v2.json` 的不可变 `build_id` 与 `artifact_sha256`；React Native 候选构建绑定 `build-identity.v2.json`（剥离 Rig 级 `target_fingerprint`）中的 `(base_ipa_sha256, main_jsbundle_sha256, assets_tree_sha256)` 纯内容摘要；
   - **外部关系证据文件（Symbol Relation Claims）**：所有静态来源记录与运行期 `unique_visibility` 观测统一写入外部 claims 文件（复用 `knowledge-claims.v1.json` 顶层信封与枚举），通过 `scope.binding_sha256` 单向引用当时的符号绑定快照，且仅在设备构建身份达到 `confirmed`（拒绝 `ambiguous` 三元组匹配）时才登记制品级 `vm_observation` 记录。
2. **单次读取冻结 `binding_sha256`**：对齐 `autophone/execution/golden_path_binding.py`，在加载绑定期一次性读取 `app-symbols.v1` 原始文件字节计算 `binding_sha256` 并写入 Run 证据；由于运行期观测写入外部 claims 文件，新增观测永不反向污染 `binding_sha256`。
3. **多版本符号文件按制品摘要自动路由**：
   - `apps/<app>/symbols/artifacts/<content_digest>.yaml` 支持多版本并存；
   - **真机 Run 期**从 `--build-id` / Package Store（或 `confirmed` `build-identity`）提取精确制品内容摘要，自动定位唯一匹配的符号文件，缺失或文件内 `artifact_identity` 摘要不一致立即 fail-closed；
   - **离线 `case lint` 期**按显式 `--build-id` 或当前 promoted 构建解析符号文件，跨版本并行回归无需修改 `profile.yaml`。
4. **转正门禁全局强制精确制品摘要 + 按需强制 `binding_sha256`**：
   - `autophone/assets/negative_control.py` 探针同步记录精确制品内容摘要与 `binding_sha256`；
   - `autophone/assets/promotion.py` 的 `discrimination_gaps()` 与 `evaluate_promotion_gate()` 全局升级为强制校验精确制品内容摘要（无论是否使用符号，缺失精确摘要的历史旧证据或同版本号异包一律判为 `discrimination_stale`）；当被评估资产使用了符号绑定时，额外强制校验 `binding_sha256` 非空且完全一致。

### 2.2 扩展 `tap_ax` 承载加载期符号解析、页面预检与唯一命中

当前执行链路存在两处缺口：
- `autophone/execution/engine/actions.py` 的 `tap_ax` 在 `query=""`（仅按 `identifier` 与 `role` 定位）时，`ambiguous_label()` 直接返回 `False`，匹配 `>= 2` 个节点时静默点击 `matches[0]`；
- `autophone/execution/engine/recipe.py` 的 `_check_params()` 尚未允许 `tap_ax` 接收 `screen_id` 与 `require_unique`。

首版实施规则（路线 A：不新增 `ACTIONS` 动词）：
1. **Recipe 步骤语法与统一加载期展开**：
   - `Recipe`（及 `Operation` 内部步骤）使用 `{"action": "tap_ax", "symbol": "ctrl.home.preferences"}`（可选保留 `label`）；静态检查严禁在同一符号步骤混写 `identifier`、`role`、`region`、`fallback_xy`、`query` 或 `enabled_only`；
   - 统一绑定加载器（供 `case lint`、准入与执行器共用）在加载期将 `symbol` 解析为冻结的 `tap_ax` 参数（`identifier`、`role`、`region`、`enabled_only`、`screen_id`、`require_unique=True`、`fallback_xy=None`），保持 1:1 `step_index`，并在步骤元数据中附带 `symbol_id`、`binding_sha256` 与声明的 `effects`。
2. **执行路径四项硬约束**：
   - **复用 `screens.yaml` 做页面前置核验（`screen_id`）**：以 `screens.yaml` 作为页面锚点唯一真源（首轮不扩新 Schema），加载期校验 `screen_id` 在 `screens.yaml` 中已定义，点击前复用 `autophone.assets.screens` 匹配器执行单帧前置核验，不匹配即报 `screen_mismatch` 阻断点击；
   - **唯一可操作命中（`require_unique`）**：帧稳定刷新 AX 快照后，目标区域内命中 `0` 个节点记 `locator_missing`，命中 `>= 2` 个节点记 `ambiguous_match`（同时修复非符号 `tap_ax` 仅按 `identifier` 定位时的多命中盲点），**两种情形下 UDS 点击派发次数严格为 `0`**；
   - **禁止坐标回退**：符号绑定步骤强制 `fallback_xy=None`；
   - **禁止静默关弹窗绕过**：符号绑定步骤抛出 `StepFailure` 时，禁止触发 `recipe.py` 的 `_dismiss_blocking_overlay_once` 自动关弹窗重试，确保遮挡与页面异常严格 fail-closed。
3. **顶层遮挡（Hit-Testability）首轮实证边界**：
   现有 `tools/guest/ax_dump.dylib` 与 `autophone/device/ax_target.py` 尚未实现 `hitTest` / `isHittable` 判定。首轮不强改 `ax_dump.dylib`，先在目标 VM 上实测顶层弹窗遮挡 `home.ai_prompt.preferences` 时的真实表现（检验 `screen_id` 单帧核验能否拦截、是否存在穿透点击），凭实测证据决定后续补采集端过滤还是前移 `guest_axruntime` 实验。

### 2.3 多副作用集合与 `lower_bound` / `unknown` 的机器可验准入

当前 `autophone/execution/effects.py` 的 `irreversible_marker()` 命中首个关键词即返回单值 `verb`（如 `"下单并支付"` 仅返回 `pay` 而漏检 `place_order`），且 `allowed_effects` 中的 `"irreversible"` 通配符会一键放行全部不可逆动作。

首版副作用契约升级为 **`verbs`（不可逆动作集合） + `completeness`（`exhaustive | lower_bound | unknown`）**：
1. **全量提取与逐项核验**：
   - 修复 `irreversible_marker()` 提取全部命中的不可逆动词，`EffectDecision` 升级为携带 `verbs: frozenset[str]` 与 `completeness`；
   - 动作声明的所有已知不可逆 `verbs` 必须**逐项**属于 `profile.allowed_effects`；**多副作用符号动作拒绝接受 `"irreversible"` 宽泛通配符**，必须在 `profile.allowed_effects` 中显式列出每一项允许的具体 `verb`，缺任一项即在派发前阻断。
2. **`exhaustive` vs `lower_bound` / `unknown` 的准入分级**：
   - `exhaustive`：已完整说明副作用边界（`verbs: []` 表示确认只读无外部写入），满足 `allowed_effects` 后可进入正式回归（`active`）；
   - `lower_bound` 与 `unknown`：**正式回归（`active`）与 `case lint`（active 资产审计）一律硬阻断**；仅允许在同时满足以下两项机器可验条件的 `draft` 试跑中执行：
     1. CLI 显式传入 `--trial-draft-recipes --confirm-trial-effects`；
     2. `profile.yaml`（`app-profile.v1.json`）显式声明隔离测试数据标记（`trial_data_isolation: "isolated"`，缺省视为未隔离并阻断）。

### 2.4 定位、产品预期与资产转正分别取证（及跨版本复用矩阵）

1. **三件事各自独立取证**：
   - 稳定 `symbol_id` 负责在界面改版时稳定找到操作入口；
   - `Case` 独立规定产品预期（现有 OCR / AX 断言继续承担用户可见文案的独立核验，严禁拿被测包提取词表自动覆盖断言预期）；
   - 一次设备命中只向外部 claims 文件追加 `observed` 关系事实，绝不自动将 `draft` 升为 `active`；转正仍由 `promotion.py` 与 `negative_control.py` 裁决。
2. **复用 `knowledge-claims.v1.json` 规范**：
   不新增通用证据等级枚举，直接复用 `autophone/_data/schemas/knowledge-claims.v1.json` 的顶层信封（`id`、`status`、`kind`、`statement`、`provenance.source`、`evidence_level`、`evidence_state`、`scope`、`evidence_refs`），以 `kind` 区分 `symbol.locator_binding`、`symbol.unique_visibility`、`symbol.effect_set`，并在 `scope` 中记录 `symbol_id`、`artifact_sha256`（或 RN 内容摘要）、`binding_sha256`、`screen_id` 与 `ax_backend`。
3. **跨版本复用与重验矩阵**：

| 资产或结论维度 | 下次发版能否跨包复用 | 失效触发条件（满足任一即需重验） |
| :--- | :--- | :--- |
| **逻辑 `symbol_id` 与 `Case` 业务意图** | **直接复用** | 业务流程本身重构或需求变更 |
| **定位器绑定（`app-symbols.v1`）** | 仅同制品内容自动路由命中；跨版本可由 L2 清单重新生成或继承为待验候选 | 制品内容摘要变更或绑定文件内容修订（`binding_sha256` 改变） |
| **页面唯一可见性（`symbol.unique_visibility`）** | **不跨异包、不跨后端静默继承** | 制品内容摘要、前置 `screen_id`、`ax_backend` 或 `binding_sha256` 任一变更 |
| **副作用边界（`symbol.effect_set`）** | 随构建清单或人工审计契约核对复用 | 制品内容摘要或构建清单/人工审计声明变更 |
| **断言区分力证据（`negative_control`）** | **全局按精确制品内容重验** | 精确制品内容摘要、`Recipe`/`Operation` 身份或符号 `binding_sha256` 任一变更（或旧证据缺失精确制品摘要），均由 `promotion.py` 判 `discrimination_stale` |

### 2.5 页面与控件匹配之外：业务对象身份与 API / 数据模型判定边界

> [!IMPORTANT]
> **范围边界声明（API 与数据模型联合判定属于阶段二独立专项）**：
> 即使目标 `screen_id` 与控件唯一命中全部成立（例如点进了另一家门店，或点击保存偏好后前端仅局部刷新而未落盘），**单靠页面 UI 也不足以证明操作了正确的业务对象**。
> - **首轮锚定方式**：通过独立于被测包的测试输入数据固定预期业务实体配合页面特征文案构造“目标页面正确、业务对象错误”反例，验证 `Case` 断言不止检查页面公共壳子。
> - **阶段二必补缺口**：真正的业务对象正确性需由 **页面 UI + `api_capture`/`api_mark` 请求响应载荷 + `fixture_readback` 数据回读** 联合裁决（现有 `field-binding.v1.json` 仅描述不含实体 ID 的类级映射）。

---

## 三、`app-symbols.v1` 不可变契约与外部证据示意（基于 `Food Ordering`）

### 3.1 不可变制品符号绑定文件
路径示例：`apps/food_ordering/symbols/artifacts/14282f675363.yaml`（**仅含不可变绑定声明，原始文件字节 SHA-256 即 `binding_sha256`**）：

```yaml
schema: autophone.app-symbols/1
app: food_ordering

# 一、制品内容身份头：仅含与 Rig 无关的不可变内容摘要（严禁写入 target_fingerprint）
artifact_identity:
  kind: ipa_build                 # ipa_build | rn_candidate
  bundle_id: "com.autophone.foodorderingdemo.v2"
  short_version: "1.0.0"
  build_version: "1"
  build_id: "bld-14282f675363"
  artifact_sha256: "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
  # 若 kind: rn_candidate，则绑定 build-identity.v2.json 中的纯内容三元组：
  # base_ipa_sha256 / main_jsbundle_sha256 / assets_tree_sha256

# 二、控件绑定表：每个逻辑符号在当前精确制品下的页面作用域、唯一命中有约束定位器与多副作用声明
controls:
  ctrl.home.preferences:
    kind: button
    scope:
      screen_id: "S01_home"       # 对应 screens.yaml 中的页面锚点 ID
      region: [0.05, 0.15, 0.95, 0.90]
    locator:
      kind: ax_identifier
      identifier: "home.ai_prompt.preferences"
      role: button
      enabled_only: true
      require_unique: true
    effects:
      verbs: []                   # 多副作用示例：["place_order", "pay"]
      completeness: exhaustive    # exhaustive | lower_bound | unknown
```

### 3.2 外部符号关系证据文件（复用 `knowledge-claims.v1.json`）
路径示例：`apps/food_ordering/symbols/claims.json`（运行期追加观测不改变上方符号文件的 `binding_sha256`；`scope.ax_backend` 精确记录产出该观测时的后端，当前默认为 `guest_axruntime`，显式灰盒运行为 `frida_inproc`，跨后端证据严禁混用）：

```json
{
  "schema_version": "v1",
  "app": "food_ordering",
  "claims": [
    {
      "id": "clm-sym-001",
      "status": "active",
      "kind": "symbol.locator_binding",
      "statement": "ctrl.home.preferences binds to ax_identifier home.ai_prompt.preferences on S01_home",
      "evidence_level": "static_hint",
      "evidence_state": "current",
      "provenance": { "source": "l2_build_manifest" },
      "scope": {
        "symbol_id": "ctrl.home.preferences",
        "artifact_sha256": "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58",
        "binding_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
      },
      "evidence_refs": ["manifests/autophone-symbols.json"]
    },
    {
      "id": "clm-sym-002",
      "status": "active",
      "kind": "symbol.unique_visibility",
      "statement": "ctrl.home.preferences uniquely matched 1 enabled button on S01_home",
      "evidence_level": "observed",
      "evidence_state": "current",
      "provenance": { "source": "vm_observation" },
      "scope": {
        "symbol_id": "ctrl.home.preferences",
        "artifact_sha256": "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58",
        "binding_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "screen_id": "S01_home",
        "identity_state": "confirmed",
        "ax_backend": "frida_inproc"
      },
      "evidence_refs": [
        "runs/run_20261126_01/build-identity.json",
        "runs/run_20261126_01/shots/R3_01.ax.json"
      ]
    }
  ]
}
```

### 3.3 `Recipe` 引用示例（路线 A：复用 `tap_ax`）

```yaml
steps:
  - action: tap_ax
    symbol: ctrl.home.preferences
    label: 打开口味偏好设置
  - action: assert_present
    queries:
      - "偏好"
```

---

## 四、第一轮验证实验设计

### 4.1 已确认实验路径与工具链深度
- **推进顺序（Track A）**：先更新文档并落地全部核心 Schema、加载/执行/准入/转正代码及离线 6 项反例自动化单测；随后基于本机 `Food Ordering` 源码构建（或 RN candidate 更新 `main.jsbundle`）准备第二版制品，在同一台 `vphone` 上跑完 L2 两版跨版本对照与同包 L1 盲测。
- **首轮工具链深度**：
  1. **L2 薄清单导入**：将构建侧符号清单 JSON + Package Store 制品摘要转为不可变 `app-symbols.v1` 与初始 `static_hint` claims；
  2. **L1 最小 RN 盲提取器**：仅解析未加密 IPA 的 `main.jsbundle` 提取 `testID` / `accessibilityIdentifier` 候选（副作用自动标 `verbs: [], completeness: unknown`），暂不做原生 Mach-O 反编译。

### 4.2 首轮实验靶点：`Food Ordering`“首页 → 调偏好”
基于 `FoodOrdering-1.0.0-1.ipa`（`14282f675363...`）的 `main.jsbundle` 中稳定标识 `home.ai_prompt.preferences`（导航至 `/preferences`），补齐 `S01_home` 屏幕资产，并在两版构建上让**手写 `Recipe`** 与**符号绑定 `Recipe`** 执行同一导航任务并使用相同独立断言，完成 L2 跨版本验证与同包 L1 盲测对照。

### 4.3 必测反例清单（离线自动化单测 + 目标 VM 实证）

1. **改文案不改 ID 反例（定位与产品文案断言分治）**：修改入口按钮显示文案但保留 `home.ai_prompt.preferences` → 导航至 `/preferences` 的符号业务用例继续通过；专门断言旧按钮文案的用例按预期判 `failed`。
2. **重复 ID 歧义零触控反例（防点错控件）**：构造同一 `identifier` + `role` 返回 `0`、`1`、`2` 个节点 → `0` 个节点报 `locator_missing`，`2` 个节点报 `ambiguous_match`，两种情形下 UDS 点击派发次数均严格为 `0`。
3. **同版本号异制品与缺失摘要反例（防用错包与防旧证据混用）**：构造版本号同为 `1.0.0 (1)` 但制品摘要不同的新包（或仅含 `subject_build_key` 的历史旧探针） → 新包不继承旧包 `unique_visibility` 观测，且携带旧包证据申请转正时被 `promotion.py` 判 `discrimination_stale` 阻断；修改符号文件使 `binding_sha256` 变化亦同样判 `discrimination_stale`。
4. **跳过导航与公共顶栏弱断言反例（防无区分力资产转正）**：将后置断言设为首页与目标页共有的公共顶栏元素，使用 `negative_control.py` 的 `skip_step` 跳过导航步骤 → 弱断言仍 `passed`，负对照识别出 `discrimination_toothless` 并阻断转正。
5. **多副作用、通配符拒绝与 `lower_bound` 隔离准入反例**：
   - 动作声明 `["place_order", "pay"]`，Profile 仅允许 `["pay"]`（或仅配置宽泛 `"irreversible"` 通配符） → 派发前阻断；
   - 动作声明 `completeness: lower_bound` 且已知项全部在允许集内 → `active` 准入硬阻断；`draft` 试跑缺 `--confirm-trial-effects` 或 `profile.yaml` 缺 `trial_data_isolation: "isolated"` 时亦硬阻断，仅两者齐备时放行试跑。
6. **遮挡与业务对象错配核验**：
   - **弹窗遮挡实证**：在目标 VM 上触发顶层弹窗遮住唯一按钮 `home.ai_prompt.preferences`，验证符号步骤禁用自动关弹窗绕过后的真实拦截/穿透表现；
   - **目标页面正确、业务对象错误反例**：由独立测试数据固定预期业务实体，验证进错对象但页面结构合法时断言准确判败，衔接阶段二 API / 数据模型联合校验。

### 4.4 决定是否扩大投入的双分母与维护成本指标
- **双分母覆盖率**：
  1. **候选可用率**：`真机唯一可点命中数 / 提取候选总数`；
  2. **业务目标覆盖率**：`真机唯一可点命中数 / 独立列出的必测控件与业务动作总数`。
- **跨版本手写 vs 符号对照账本**：在两版 App 上执行同一任务与相同断言，分别记录手写 `Recipe` 与符号 `Recipe` **覆盖目标数、跨版修改处数、人工耗时**（区分“稳定 `identifier` 自身收益”与“符号绑定层在多处复用、跨包断链静检和集中多副作用声明上的增量收益”）。
