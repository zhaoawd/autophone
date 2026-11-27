# 编写用例与 Recipe（Typed-IR）

**简体中文** | [English](authoring-cases.en.md)

Autophone 将“测什么”（自然语言 `Case`）与“怎么执行”（确定性 `Recipe` / `Operation`）分离管理。正式回归运行期严格执行已静态校验的 Typed-IR 步骤，不依赖 AI 临场决策。

## 目录结构

所有 App 资产位于 `$AUTOPHONE_APP_ASSET_DIR/apps/<app>/` 下（完整示例见 `examples/ios-settings-smoke/` 与 `examples/food-ordering/`）：

```text
apps/<app>/
├── profile.yaml            # App Profile（bundle_id、reset 策略等）
├── cases/
│   └── cases.yaml   # Case Registry（schema_version: 2，默认读取名）
├── recipes/
│   ├── r1.yaml             # live Recipe（对应 active 用例）
│   └── drafts/
│       └── r2.yaml         # draft Recipe（对应 draft 用例）
└── operations/
    └── operations.yaml     # 可复用状态变换 Operation（可选）
```

各文件的完整字段字典由代码自动生成，见 [Schema 参考手册](../reference/schemas.md)。

## 1. App Profile（`profile.yaml`）

```yaml
schema_version: v1
app: my_app
product_key: my_app
bundle_id: com.example.myapp
display_name: My App
env: verification
lifecycle: active
reset: relaunch
capabilities:
  api_capture:
    enabled: false
    ready_markers:
      - Home
      - 首页
```

> [!TIP]
> 运行期启动就绪门禁不信任系统前台进程名，必须通过 `capabilities.api_capture.ready_markers` 声明至少一个启动后可见的 OCR 文案标记；否则真机回归会在准备阶段判为 `app_ui` 未就绪。

## 2. Case Registry（`cases/*.yaml`）

```yaml
meta:
  title: My App Smoke
  app: my_app
  schema_version: 2
  total: 1
  next_seq: 2
cases:
  - id: R1
    revision: 1
    status: draft
    automation: ui_ready
    case_type: 功能测试
    test_point: 首页关键入口可见
    owner: qa
    module: 首页
    item: 启动后首页展示核心入口文案
    login_precondition: any
    steps:
      - 启动应用并等待首页稳定
    expects:
      - 页面显示目标文案
```

## 3. Recipe（`recipes/r1.yaml`）

Recipe 是封闭的二选一步骤列表：每步只能是白名单 `action`（调用注册 Verb）或已注册的 `operation`，不包含分支循环脚本逻辑。所有可用 Verb 与参数约束见 [Verb 参考手册](../reference/verbs.md)。

```yaml
id: R1
steps:
  - action: wait_text
    query: 通用
    timeout_s: 10
  - action: tap_text
    query: 通用
  - action: assert_present
    queries: ["关于本机", "软件更新"]
    wait_s: 5
```

> [!IMPORTANT]
> 每条 Recipe 必须至少包含一个 `assert_*` 动作；空执行或零断言正常返回在运行期会被判定为 `error`，防止假绿（见[回归运行期契约](../concepts/runtime-contract.md)）。

## 4. 静态校验与试跑

在不连接 VM 的情况下先跑静态校验：

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/app-assets"
autophone app case lint my_app
```

静态校验通过后，可在指定 Rig 上显式试跑草稿用例并查看生成的 HTML 证据报告：

```bash
autophone --app my_app --device rig-a run --ids R1 --draft-cases
```

当草稿用例连续通过验证并满足转正条件后，可将其升级为 `active` 进入默认回归集。
