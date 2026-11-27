# Recipe Verb 动作手册（自动生成）

> 本文档由 `scripts/generate_public_docs.py` 从 `autophone.execution.engine.recipe.ACTIONS` 与 `CaseContext` 自动生成，请勿手工修改。

## 坐标与约束约定

- **预览像素坐标（`px`, `py`, `px1`, `py1`, `px2`, `py2`, `fallback_xy`）**：使用预览帧像素坐标（默认竖屏 `430×932`，执行层按设备缩放比换算为原生像素）。
- **归一化区域（`region`, `regions`）**：`[nx0, ny0, nx1, ny1]`，取值范围 `[0.0, 1.0]`，与屏幕物理分辨率无关。
- **硬件按键白名单（`press_key.name`）**：`home`、`power`、`volup`、`voldown`。
- **静态校验**：所有步骤在触达设备前均由 `validate_recipe()` 校验动作名、必填参数、禁止未知字段与参数类型。

## 可用 Verb 总览

| Verb | 必填参数 | 说明 |
| --- | --- | --- |
| [`assert_absent`](#assert-absent) | `query` | — |
| [`assert_present`](#assert-present) | `queries` | Every query must be found. Polls screenshot+OCR up to `wait_s` so a slow-loading sub-page doesn't fail vacuously on the first frame (set wait_s=0 for a strict single-frame check). This is the executable form of 「预期结果」. |
| [`assert_present_enhanced`](#assert-present-enhanced) | `queries` | assert_present 的增强 OCR 变体：每帧先做灰阶拉伸再识别。用于浅灰文字 + 水印导致裸 OCR 返回 0 box 的页面（§协议规则）。语义与 assert_present 一致。 |
| [`assert_region_nonempty`](#assert-region-nonempty) | `regions` | 每个 (region, label) 的归一化区域内，增强 OCR 至少命中一个 box（conf≥阈值）。 |
| [`assert_remains_absent`](#assert-remains-absent) | `query` | Continuously require ``query`` to stay absent for an observation window. |
| [`assert_screen`](#assert-screen) | `screen_id`、`screen_asset`、`screen_sha256` | Assert an active Screen on a fresh required frame, bound to exact asset bytes. |
| [`handle_dialog_if_present`](#handle-dialog-if-present) | `query`、`tap_query` | Conditional branch (§4.1): if `query` is on screen, tap `tap_query`; otherwise skip silently. Never a hard failure on absence — lets one script cover both the 200m-inside and 200m-outside routes. |
| [`manual_pause`](#manual-pause) | `prompt` | Pause a case for a human action, holding the screen lock until resume. |
| [`press_key`](#press-key) | `name` | 按一次硬件键（home/power/volup/voldown）。 |
| [`swipe_xy`](#swipe-xy) | `px1`、`py1`、`px2`、`py2` | Swipe from preview (430×932) coord (px1,py1) to (px2,py2) over `ms`. The geometric escape hatch for scroll/drag gestures that have no semantic anchor (carousel paging, pull-to-refresh, list scroll) — the swipe analogue of tap_xy. Both endpoints are preview coords, ×3 to native for the UDS swipe; native is derived per-call and never re-fed (same contract as tap_xy). |
| [`tap_ax`](#tap-ax) | 无 | Accessibility-locate a target and tap its native center — the structural peer of tap_text (§七). Reaches what OCR can't: icon-only buttons (no visible text, matched by `identifier` or an a11y `label`), and role/region disambiguation of a repeated label. |
| [`tap_text`](#tap-text) | `query` | OCR-locate `query`, tap its native center. `region` (normalized) keeps a duplicate label (two「确定」) unambiguous. `min_confidence` overrides the ctx default for a known-but-low-confidence label. Re-shoots between retries; `retry_delay_s` lets slow async lists populate between re-shoots. |
| [`tap_xy`](#tap-xy) | `px`、`py` | Tap a preview (430×932) coordinate. The escape hatch for textless icon targets (profile entry, back chevron) until AX pull lands (§六/§七). Prefer tap_text everywhere a label exists. |
| [`type_text`](#type-text) | `text` | Clipboard write (NOT keyboard input) — `type` is UIPasteboard setString (CLAUDE.md). The caller must have focused a field and arranged a paste; this only stages the text. Long tail → TYPE_TIMEOUT_S. |
| [`wait_text`](#wait-text) | `query` | Poll screenshot+OCR until `query` appears or timeout — replaces a fixed sleep (§4.1). |

## Verb 参数详情

### `assert_absent`

—

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `query` | 是 | `string` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `min_confidence` | 否 | `number` | `None` |

```yaml
- action: assert_absent
  query: "网络异常"
  wait_s: 2
```

### `assert_present`

Every query must be found. Polls screenshot+OCR up to `wait_s` so a slow-loading sub-page doesn't fail vacuously on the first frame (set wait_s=0 for a strict single-frame check). This is the executable form of 「预期结果」.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `queries` | 是 | `non-empty list of strings` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `min_confidence` | 否 | `number` | `None` |
| `wait_s` | 否 | `number` | `6.0` |
| `poll_ms` | 否 | `int` | `600` |

```yaml
- action: assert_present
  queries: ["通用", "关于本机"]
  wait_s: 5
```

### `assert_present_enhanced`

assert_present 的增强 OCR 变体：每帧先做灰阶拉伸再识别。用于浅灰文字 + 水印导致裸 OCR 返回 0 box 的页面（§协议规则）。语义与 assert_present 一致。

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `queries` | 是 | `non-empty list of strings` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `min_confidence` | 否 | `number` | `None` |
| `level` | 否 | `string` | `'38%,55%'` |
| `wait_s` | 否 | `number` | `6.0` |
| `poll_ms` | 否 | `int` | `600` |

```yaml
- action: assert_present_enhanced
  queries: ["隐私政策"]
  wait_s: 3
```

### `assert_region_nonempty`

每个 (region, label) 的归一化区域内，增强 OCR 至少命中一个 box（conf≥阈值）。

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `regions` | 是 | `list of [[4 numbers], label-string]` | — |
| `min_confidence` | 否 | `number` | `0.3` |
| `level` | 否 | `string` | `'38%,55%'` |
| `wait_s` | 否 | `number` | `6.0` |
| `poll_ms` | 否 | `int` | `600` |

```yaml
- action: assert_region_nonempty
  regions:
    - [[0.05, 0.10, 0.95, 0.40], "header"]
  wait_s: 3
```

### `assert_remains_absent`

Continuously require ``query`` to stay absent for an observation window.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `query` | 是 | `string` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `min_confidence` | 否 | `number` | `None` |
| `duration_s` | 否 | `positive number` | `3.0` |
| `poll_ms` | 否 | `positive int` | `300` |

```yaml
- action: assert_remains_absent
  query: "加载中"
  duration_s: 3
  poll_ms: 500
```

### `assert_screen`

Assert an active Screen on a fresh required frame, bound to exact asset bytes.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `screen_id` | 是 | `string` | — |
| `screen_asset` | 是 | `string` | — |
| `screen_sha256` | 是 | `string` | — |
| `retries` | 否 | `int` | `3` |

```yaml
- action: assert_screen
  screen_id: settings_home
  wait_s: 3
```

### `handle_dialog_if_present`

Conditional branch (§4.1): if `query` is on screen, tap `tap_query`; otherwise skip silently. Never a hard failure on absence — lets one script cover both the 200m-inside and 200m-outside routes.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `query` | 是 | `string` | — |
| `tap_query` | 是 | `string` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `min_confidence` | 否 | `number` | `None` |

```yaml
- action: handle_dialog_if_present
  query: "允许"
  tap_query: "允许"
```

### `manual_pause`

Pause a case for a human action, holding the screen lock until resume.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `prompt` | 是 | `string` | — |
| `timeout_s` | 否 | `number` | `900.0` |
| `poll_ms` | 否 | `int` | `500` |

```yaml
- action: manual_pause
  prompt: "请人工核对当前画面后继续"
```

### `press_key`

按一次硬件键（home/power/volup/voldown）。

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `name` | 是 | `one of home/power/volup/voldown` | — |

```yaml
- action: press_key
  name: home
```

### `swipe_xy`

Swipe from preview (430×932) coord (px1,py1) to (px2,py2) over `ms`. The geometric escape hatch for scroll/drag gestures that have no semantic anchor (carousel paging, pull-to-refresh, list scroll) — the swipe analogue of tap_xy. Both endpoints are preview coords, ×3 to native for the UDS swipe; native is derived per-call and never re-fed (same contract as tap_xy).

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `px1` | 是 | `int` | — |
| `py1` | 是 | `int` | — |
| `px2` | 是 | `int` | — |
| `py2` | 是 | `int` | — |
| `ms` | 否 | `positive int` | `300` |
| `label` | 否 | `string` | `''` |

```yaml
- action: swipe_xy
  px1: 215
  py1: 720
  px2: 215
  py2: 260
  ms: 300
```

### `tap_ax`

Accessibility-locate a target and tap its native center — the structural peer of tap_text (§七). Reaches what OCR can't: icon-only buttons (no visible text, matched by `identifier` or an a11y `label`), and role/region disambiguation of a repeated label.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `query` | 否 | `string` | `''` |
| `role` | 否 | `string` | `None` |
| `identifier` | 否 | `string` | `None` |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `enabled_only` | 否 | `bool` | `True` |
| `fallback_xy` | 否 | `[px,py] of ints` | `None` |
| `label` | 否 | `string` | `''` |
| `screen_id` | 否 | `string` | `None` |
| `require_unique` | 否 | `bool` | `False` |

```yaml
- action: tap_ax
  label: "通用"
  role: Button
```

### `tap_text`

OCR-locate `query`, tap its native center. `region` (normalized) keeps a duplicate label (two「确定」) unambiguous. `min_confidence` overrides the ctx default for a known-but-low-confidence label. Re-shoots between retries; `retry_delay_s` lets slow async lists populate between re-shoots.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `query` | 是 | `string` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `retries` | 否 | `int` | `3` |
| `retry_delay_s` | 否 | `non-negative number` | `0.0` |
| `min_confidence` | 否 | `number` | `None` |

```yaml
- action: tap_text
  query: "通用"
  retries: 2
```

### `tap_xy`

Tap a preview (430×932) coordinate. The escape hatch for textless icon targets (profile entry, back chevron) until AX pull lands (§六/§七). Prefer tap_text everywhere a label exists.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `px` | 是 | `int` | — |
| `py` | 是 | `int` | — |
| `label` | 否 | `string` | `''` |

```yaml
- action: tap_xy
  px: 215
  py: 420
```

### `type_text`

Clipboard write (NOT keyboard input) — `type` is UIPasteboard setString (CLAUDE.md). The caller must have focused a field and arranged a paste; this only stages the text. Long tail → TYPE_TIMEOUT_S.

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `text` | 是 | `string` | — |

```yaml
- action: type_text
  text: "hello"
```

### `wait_text`

Poll screenshot+OCR until `query` appears or timeout — replaces a fixed sleep (§4.1).

| 参数 | 必填 | 类型约束 | 默认值 |
| --- | --- | --- | --- |
| `query` | 是 | `string` | — |
| `region` | 否 | `[nx0,ny0,nx1,ny1] of numbers` | `None` |
| `timeout_s` | 否 | `number` | `8.0` |
| `poll_ms` | 否 | `int` | `600` |
| `min_confidence` | 否 | `number` | `None` |

```yaml
- action: wait_text
  query: "关于本机"
  timeout_s: 10
```
