"""ai_convert_api.py — AI 降解器（P2）：集成用例 xlsx 一行 → 三份草稿。

集成用例转化 §六(2)。与 AI-5 `ai_convert`（NL 用例 → 带断言 recipe）对偶：那条把
**预期钉成 recipe 内的 assert_* 步骤**；这条把一行**集成用例**拆成正交两面 + 外部真值三份草稿：

  ① case 草稿           —— 驱动面（typed-IR operation 引用序列，**不含断言**，§五）→ cases 库
  ② api_expectations 草稿 —— 断言面（接口断言中文+JSONPath 降解成 (path,op,value)）→ 断言库
  ③ fixtures 草稿        —— 外部真值（满减门槛/优惠金额，§四）→ api_fixtures.yaml

铁律（对齐 AI-4/AI-5）：
1. **产物恒 draft**；case 引用的 operation 未注册时在缺口字段中保留证据。AI 绝不自行入回归库，
   转正走人审 + 冻结门。
2. **拒写脏**：AI 回包先过内容 schema，再过 §六 静态闸——
   · api_expectations 每条 assert 的 JSONPath 过 `static_validate_expect_entries`（语法错/op 非白名单 fail-closed）；
   · case steps 不得含 assert_*（断言属于用例、不下沉 recipe step）——AI 违规时确定性剥离、
     原文进 needs_manual 供人工核对，剥离后无可执行步骤才整行拒收；
   · expectations 引用的每个 ${var} 必须在 fixtures 里有键（值可为 null=待填），否则缺口不浮现 → fail-closed。
   任一不过 → ok=False，不落任何文件。
   AI 回包的**确定性形态修复**（漏 resp. 前缀补齐、可省略面写成 null 删键、assert 步剥离）不算写脏——
   修复后仍要过全部静态闸，且每次修复以 slug 记进结果的 repairs 供审计。
   瞬态失败（超时/截断 JSON/schema 漂移）按 retries 重打，不因单行瞬态废整批。
3. **跨元素关系断言不硬塞**（§三.4）：AI 判 needs_manual，原文留在草稿的 raw.needs_manual 供人工接手。
4. **缺操作不臆造**（§五）：referenced operation 是否已注册由**本模块按真实注册表核对**（不轻信 AI），
   缺则 case 仍落 draft，缺的 op 名结构化报出（P3 建操作库消费）。
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from autophone.assets import API_EXPECTATIONS_SPEC
from autophone.paths import APP_ASSET_ROOT
from autophone.assets import cases as cases_store
from autophone.assets import store as asset_store
from autophone.application.ai import AiRequest, CommandRunner, _subprocess_runner, invoke
from autophone.application.ai_draft import _ACTION_STEP, _OPERATION_STEP, _capability_brief
from autophone.assets.apps import load_app_profile
from autophone.assets.policy import assert_assets_writable
from autophone.execution.multicase_schedule import (
    build_role_hints,
    infer_group_key,
    infer_order_hints,
    suite_profile_from_convert_results,
)
from autophone.observation.frida import api_oracle

# ── 内容 schema：AI 恰好回一支三面草稿对象 ────────────────────────────────────
_MATCH = {
    "type": "object", "additionalProperties": False, "minProperties": 1,
    "properties": {"method": {"type": "string"}, "url_glob": {"type": "string"}},
}
_EXPECT_ENTRY = {
    "type": "object", "additionalProperties": False, "required": ["match"],
    "properties": {
        "step": {"type": "string"},
        "bind": {"enum": ["first", "last"]},
        "match": _MATCH,
        "assert": {"type": "object"},
    },
}
_CASE = {
    "type": "object", "additionalProperties": False, "required": ["module", "item", "steps"],
    "properties": {
        "module": {"type": "string"},
        "item": {"type": "string"},
        "external_id": {"type": ["string", "number", "null"]},
        "steps": {"type": "array", "minItems": 1,
                  "items": {"oneOf": [_ACTION_STEP, _OPERATION_STEP]}},
    },
}
_CONVERT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["case"],
    "properties": {
        "case": _CASE,
        "api_expectations": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "expect": {"type": "array", "items": _EXPECT_ENTRY},
                "forbid": {"type": "array", "items": _EXPECT_ENTRY},
            },
        },
        "fixtures": {"type": "object"},           # var → 标量真值（未知填 null）
        "needs_manual": {"type": "array", "items": {"type": "string"}},
    },
}

_SYSTEM = (
    "你是 autophone 的「集成用例降解」agent。把给定的**一行集成测试用例**拆成正交的三份草稿——"
    "驱动(case)、接口断言(api_expectations)、外部真值(fixtures)——并**只返回一个 JSON 对象**。\n"
    "\n【驱动 case.steps】把「场景步骤」映射成 typed-IR operation 引用序列，每个 step 恰好二选一：\n"
    "  - {\"operation\": <name>, \"args\": {…}}   引用一条有状态操作（args 的键等于其 kwarg）\n"
    "  - {\"action\": <verb>, <参数名>: <值>}      调一个白名单 action verb\n"
    "  **禁止**在 case.steps 里放任何 assert_* —— 断言属于用例、只进 api_expectations，不下沉 recipe step。\n"
    "  缺操作就引用它的名字（下方白名单没有也照引，本机会按真实注册表核对并记录操作缺口），**不要**编 action 去凑。\n"
    "\n【断言 api_expectations】把「接口断言」（中文 + 半形式化 JSONPath）降解成 expect 条目：\n"
    "  expect[] = {step?, bind?, match:{method?,url_glob}, assert:{<path>: <spec>}}\n"
    "  · path 只用 JSONPath 子集：a.b / a.0.b / a[*] / a[?(@.f=='v'&&@.g==h)]，键前缀 status|resp.|req.\n"
    "  · 原表 `items[*].[?(@.f=='v')]` 的 [*] 前缀冗余，**规整**成 `items[?(@.f=='v')]`\n"
    "  · spec 用对象形 {op, value?}，op ∈ eq|ne|empty|exists|contains；数值真值不在响应里就写 \"${var}\" 并在 fixtures 声明该 var\n"
    "  · 「记录为空」→ {op: empty}；「存在/不为空」→ {op: exists}；「code/errorCode = 0」→ {status:200 或 resp.code:{op:eq,value:0}}\n"
    "  · 「提示还差xx」是**逐条文本子串**匹配：目标取 discountTipList[*]（数组逐元素）配 {op: contains, value: '还差'}，"
    "不要对整个 list 用 contains（那是精确成员、会假阴）\n"
    "  · 不该出现的接口放 forbid[]（同 match 形态）\n"
    "  · 购物车确认/营销终态断言通常会命中多次，必须写 bind:\"last\"；只有明确要首条时才写 bind:\"first\"\n"
    "\n【跨元素关系断言】三类高频模式**优先用下面的具名 op**表达，不要一律甩去 needs_manual：\n"
    "  · 「第N件价格=原价×倍率」（如第二件半价/第二件加倍）→ op:half_price_pair，"
    "path 指向该组元素的数组（如 items 或过滤出的子数组），"
    "{price_field, base_field, factors:[按数组顺序每元素的倍率，如 [1,2] 或 [1,0.5]]}\n"
    "  · 「赠品的 relationId/关联id = 加购餐品的 id」→ op:gift_relation，"
    "path 指向元素数组，{gift_filter:{字段:值，用来在数组里定位赠品元素，如 {labelType:1}}, "
    "link_field:赠品元素上存关联id的字段名, target_field:被关联餐品上作为id的字段名}\n"
    "  · 「计算型金额=其它字段的算术」（如 promotionAmount=原价-展示价，或=原价×折扣率）→ op:computed_amount，"
    "path 指向被验证的金额字段，{formula:'仅含+-*/()的算式，变量名任取', "
    "refs:{算式里每个变量名: 对应的 JSONPath（同样带 resp./req. 前缀）}}\n"
    "  这三个 op 都只能比较**同一次调用内**的字段/元素；如果关系跨两次调用（如「加购后另一件恢复原价」"
    "要对比加购前后两次响应），无法用它们表达，才落 needs_manual。\n"
    "\n【外部真值 fixtures】断言里每个 ${var} 都要在 fixtures 里出现一个键；知道值就填（注意单位元/分由响应字段定），"
    "不知道填 null 让人工补。没有外部真值就省略 fixtures。\n"
    "\n【needs_manual】只有跨调用/无法用上面三个跨元素 op 表达的关系断言才放这里"
    "（如「加购后第一件恢复原价」这种要对比两次响应的），把断言原文放进 needs_manual 数组，别写进 assert。"
    "\n【操作命名】case.steps 引用 operation 前，先看下方白名单能不能靠换 args 表达这个能力——"
    "「进店后去结算/查看购物车」用已注册的 confirm_cart；「选预约取餐日期/时段」用 select_booking_time；"
    "「用一张券」用 use_coupon；不要因为场景措辞不同就为同一能力另起新名（如 goto_cart/enter_cart/"
    "view_cart 都应写成 confirm_cart）。若确实是白名单没有的新能力，就近用业界通用动词命名"
    "（加购一件商品统一叫 add_to_cart，减购/移除一件叫 decrease_cart_item），同一份材料里对同一能力"
    "全程用同一个名字——名字发散会让「缺哪些操作」的统计失真，拖慢后续建操作库的排期。"
)


def _norm_row(row: dict[str, Any]) -> dict[str, Any]:
    """把 xlsx 一行按表头文字归一成降解器吃的字段（营销/点餐两表列名不同，按语义取）。"""
    def pick(*names: str) -> str:
        for n in names:
            v = row.get(n)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    module = " / ".join(x for x in [pick("一级模块"), pick("二级模块"), pick("三级模块")] if x)
    return {
        "module": module,
        "item": pick("自动化用例场景名称", "场景名称"),
        "desc": pick("场景描述"),
        "source_steps": pick("场景步骤"),
        "business_rule": pick("业务规则"),
        "assertion": pick("接口断言"),
        "apis": pick("api接口（场景业务核心接口）", "涉及接口（场景业务核心接口）", "涉及接口"),
        "downstream": pick("下游接口"),
        "external_id": pick("自动化ID"),
    }


def read_material_rows(path: Path) -> list[dict[str, Any]]:
    """读集成用例 xlsx，返回归一后的行（跳过全空行）。

    ``external_id`` 始终保留源表的自动化编号；无编号的 P0、外送、点餐来源不伪造
    编号，而是用 ``source_ref``（文件 + 工作表 + Excel 行）作可追溯、可断点续跑的
    来源键。按表头文字定位列，营销/点餐/P0/外送通用。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    out: list[dict[str, Any]] = []
    for excel_row, raw in enumerate(rows[1:], start=2):
        if not any(c not in (None, "") for c in raw):
            continue
        row = {header[i]: raw[i] for i in range(min(len(header), len(raw)))}
        norm = _norm_row(row)
        if norm["item"]:
            norm["source_ref"] = f"{path.name}#{ws.title}!{excel_row}"
            out.append(norm)
    return out


def _build_prompt(
    row: dict[str, Any],
    brief: str,
    *,
    cluster_context: list[dict[str, Any]] | None = None,
) -> str:
    parts = [
        f"一级/二级模块：{row['module']}",
        f"用例场景名称：{row['item']}",
        f"场景描述：{row['desc'] or '（无）'}",
        f"场景步骤（来源原文）：{row.get('source_steps') or '（无）'}",
        f"业务规则：{row['business_rule'] or '（无）'}",
        f"接口断言（原文）：\n{row['assertion'] or '（无）'}",
        f"涉及接口：\n{row['apis'] or '（无）'}",
    ]
    if row.get("downstream"):
        parts.append(f"下游接口：\n{row['downstream']}")
    if row.get("external_id"):
        parts.append(f"自动化ID（存 raw.external_id，不当主键）：{row['external_id']}")
    parts.append("把它降解成三份草稿（case / api_expectations / fixtures）；同次调用内的跨元素关系断言"
                 "优先用具名 op（half_price_pair/gift_relation/computed_amount）表达，只有跨调用/无法用"
                 "它们表达的关系才放 needs_manual。")
    if cluster_context:
        parts.append("同簇参考行（用于统一 fixtures 真值、命中/未命中方向和调度 group_key；仍只输出当前行）：")
        for peer in cluster_context[:8]:
            parts.append(
                f"- {peer.get('external_id') or '?'} | {peer.get('item') or ''} | "
                f"{peer.get('business_rule') or ''}"
            )
    parts.append("\n" + brief)
    return "\n".join(parts)


# 别名归一（统计层）：加购/减购这两簇的目标能力
# （add_cart_amount）带必填 kwarg（item/category/category_marker），不像 confirm_cart/
# select_booking_time/use_coupon 那样零参可无脑注册别名
# ——AI 挂的 args 可能与执行契约不匹配，盲目注册别名会让缺口在真实执行时才暴露。
# 所以这两簇只做**展示层**归一：missing_operations 统计里折叠成同一个
# canonical 名字，供 P3 建操作库排期看真实缺口个数；case.steps 本身不改，
# 缺口继续写入 draft 的 readiness 证据。canonical 名取自 285 行批转化的历史统计。
_MISSING_OP_ALIASES: dict[str, str] = {name: "add_to_cart" for name in (
    "add_blacklist_items", "add_items_to_threshold", "add_non_promo_items_below_threshold",
    "add_non_promo_items_above_threshold",
    "add_second_half_price_item", "add_buy_one_get_one_item", "add_items_to_amount",
    "add_item_to_cart", "add_cart_item", "add_coupon_item_to_cart", "add_non_discount_item",
    "add_base_items", "add_menu_item", "add_premium_promo_item", "add_prepaid_package",
    "add_prime_item_below_threshold", "add_special_item_to_amount", "add_items_below_threshold",
    "add_non_promo_item", "add_non_promo_items",
    "reach_cart_threshold", "prepare_cart_below_threshold", "ensure_cart_amount",
)}
_MISSING_OP_ALIASES.update({name: "decrease_cart_item" for name in (
    "decrease_item_in_menu", "reduce_cart_amount", "reduce_cart_item_in_menu", "reduce_cart_item",
)})

# 平台 profile 明确禁止支付；提交订单会创建真实订单、同样不能由来源转换悄悄放行。
# 这是来源语义的确定性闸，不依赖模型是否恰好在 steps 中写出 submit/pay operation。
_ORDER_PAYMENT_ISOLATION_TOKENS = (
    "支付", "预支付", "下单", "/api/v2/order/submit", "/cashier/", "card-pay/",
)


def requires_order_payment_isolation(row: dict[str, Any]) -> bool:
    text = "\n".join(str(row.get(key) or "") for key in (
        "item", "desc", "source_steps", "apis", "downstream",
    ))
    return any(token in text for token in _ORDER_PAYMENT_ISOLATION_TOKENS)


def _canonicalize_missing_op(name: str) -> str:
    return _MISSING_OP_ALIASES.get(name, name)


def _material_cluster_key(row: dict[str, Any]) -> tuple[str, str]:
    """Best-effort material cluster key for Phase 1b report-mode conversion.

    It is intentionally only used to add peer context to prompts and summary
    rows.  The persisted case contract continues to come from each row's own
    converted output and static gates.
    """
    norm = row if "item" in row and "assertion" in row else _norm_row(row)
    group = infer_group_key([], norm) or {}
    module = str(norm.get("module") or "")
    promotion = str(group.get("promotion") or norm.get("business_rule") or norm.get("item") or "")
    return (module, promotion)


def _cluster_indexed_rows(indexed_rows: list[tuple[int, dict[str, Any]]]) -> dict[int, list[dict[str, Any]]]:
    clusters: dict[tuple[str, str], list[dict[str, Any]]] = {}
    normalized: dict[int, dict[str, Any]] = {}
    for row_index, row in indexed_rows:
        norm = row if "item" in row and "assertion" in row else _norm_row(row)
        normalized[row_index] = norm
        clusters.setdefault(_material_cluster_key(norm), []).append(norm)
    by_row: dict[int, list[dict[str, Any]]] = {}
    for row_index, norm in normalized.items():
        by_row[row_index] = clusters.get(_material_cluster_key(norm), [norm])
    return by_row


def _available_operations(app: str, *, root: Path = APP_ASSET_ROOT) -> set[str]:
    """该 app 「现在 case runner 真能跑」的 operation 名集合 = executor 注册表（register_operation）。

    只认已注册执行器（recipe.operations_for_app）——case runner 执行 operation step 时只解析这张表
    （recipe._active_operations），引用未注册的 op 真跑时会报 `unknown operation`。故操作库 yaml 里
    的纯资产 op（如 enter_takeout，只有文档没有执行器）**不**算可用：引用它的 case
    仍为 draft，但必须记录 missing_operations 和 execution_blockers（守 §五「缺操作不臆造」）。
    （把无控制流的纯 typed-IR yaml op 自动物化成 case 可跑执行器是另一条运行时能力，待单独建设——
    它牵涉 draft 是否可跑 / 参数校验 / 归因打点 / 转正门，不塞进这个可用性判定。）
    """
    from autophone.execution.engine import recipe
    from autophone.application.ai_draft import _ensure_app_ops

    _ensure_app_ops(app, root=root)
    return set(recipe.operations_for_app(app, root=root))


def _referenced_operations(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s["operation"]) for s in steps
            if isinstance(s, dict) and "operation" in s]


def _catalog_contract_blockers(app: str, steps: list[dict[str, Any]]) -> set[str]:
    """Return cart verbs whose source arguments still lack an executable fixture.

    ``add_to_cart``/``decrease_cart_item`` are deliberately registered executors, but their
    contract is narrower than the old spreadsheet prose: a call must name an approved
    ``catalog_key`` with its category/spec/locator evidence.  Treating mere registration as
    availability would incorrectly clear the readiness blocker on legacy ``item``/``item_id``
    calls even though recipe validation or device execution must reject them.
    """
    from autophone.app_plugins import AppPluginUnavailable, app_capability

    try:
        catalog = app_capability(app, "simple_cart_catalog")
    except (AppPluginUnavailable, ValueError):
        return set()
    if callable(catalog):
        catalog = catalog()

    blocked: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or "")
        if operation not in {"add_to_cart", "decrease_cart_item"}:
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        key = args.get("catalog_key")
        if not isinstance(key, str) or key not in catalog:
            blocked.add(operation)
    return blocked


def _split_setup_prefix(
    steps: list[dict[str, Any]], prefix_ops: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 `hints["shareable_prefix"]` 的建议把 case.steps 前缀摘成可执行 setup 引用。

    build_role_hints 只算出**名字**（raw.hints.shareable_prefix）；这一步只负责把建议反映到
    Case 的单跑执行结构，不能授权批量共享。共享调度另需人审后的稳定 case.scheduling 声明。
    只在 steps 开头**恰好**是 prefix_ops 这串 operation（无 action 步混在前面/中间）才拆，
    与 runner._declared_prefix_share 自己的对齐检查（`actual != prefix_ops`）同一把尺，避免
    把本该按原顺序跑的 action 步挪到 setup 后面执行。拆后 body 会清空也保守放弃（不拆）。
    """
    if not prefix_ops or len(steps) < len(prefix_ops):
        return [], steps
    head = steps[:len(prefix_ops)]
    if [s.get("operation") if isinstance(s, dict) else None for s in head] != prefix_ops:
        return [], steps
    body = steps[len(prefix_ops):]
    if not body:
        return [], steps
    setup = [{"op": s["operation"], **({"with": s["args"]} if s.get("args") else {})} for s in head]
    return setup, body


def _coerce_nullables(payload: dict[str, Any]) -> list[str]:
    """可省略面被 AI 写成 null（如 `fixtures: null`）语义就是「无」——schema 校验前删键，留 repair slug。"""
    repairs: list[str] = []
    for key in ("api_expectations", "fixtures", "needs_manual"):
        if key in payload and payload[key] is None:
            del payload[key]
            repairs.append(f"{key}_null_dropped")
    return repairs


def _strip_assert_steps(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 AI 违规塞进 case.steps 的 assert_* 步摘出来 → (可执行步, 被剥离的断言步)。"""
    kept: list[dict[str, Any]] = []
    stripped: list[dict[str, Any]] = []
    for s in steps:
        is_assert = isinstance(s, dict) and str(s.get("action", "")).startswith("assert")
        (stripped if is_assert else kept).append(s)
    return kept, stripped


def _prefix_resp_keys(entries: Any) -> int:
    """给漏写前缀的 assert 键补 `resp.`（断言目标就是响应体），返回补的个数。

    只做确定性形态修复：`status`/`resp.*`/`req.*` 不动；真撞子集边界（非法谓词等）的
    键补完前缀仍会被 static_validate_expect_entries 拒——闸门语义不变。
    """
    fixed = 0
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        assert_map = entry.get("assert") if isinstance(entry, dict) else None
        if not isinstance(assert_map, dict):
            continue
        for key in list(assert_map):
            k = str(key)
            if k == "status" or k.startswith("resp.") or k.startswith("req."):
                continue
            assert_map[f"resp.{k}"] = assert_map.pop(key)
            fixed += 1
    return fixed


def _var_only_assert_key(key: str) -> bool:
    """AI 偶发把 `${var}` 当成 assert path；变量只能出现在 value/fixture 或合法过滤右值里。"""
    k = key
    if k.startswith(("resp.", "req.")):
        k = k.split(".", 1)[1]
    return bool(api_oracle._VAR_RE.fullmatch(k.strip()))


def _bare_field_filter_issue(key: str, error: ValueError) -> bool:
    msg = str(error)
    if "不支持的过滤谓词" not in msg:
        return False
    # 只收敛实证模式 `[?(@.promotionId)]`：AI 想表达「字段存在」，但当前 JSONPath
    # 子集不支持裸存在性谓词。其它未知谓词（如 @==7）继续 fail-closed。
    return bool(re.search(r"\[\?\(@\.[\w.]+\)\]", key))


def _manual_static_issue(key: str, error: ValueError) -> bool:
    """P4a 已确认可人工接手的静态闸问题。

    这些错误说明 AI 想表达的断言不完整/越过当前子集，不应该写成半截 assert；但它们也不必
    让整行异常结束。把该 assert 摘到 needs_manual，保留驱动和其它合法 API 断言继续审计。
    未知静态错误仍 fail-closed，避免扩大写脏面。
    """
    msg = str(error)
    return _bare_field_filter_issue(key, error) or any(fragment in msg for fragment in (
        "computed_amount 缺 refs",
        "computed_amount 缺 formula",
        "half_price_pair 缺必填键",
        "gift_relation 缺必填键",
    ))


def _downgrade_manual_asserts(entries: Any, needs_manual: list[str], repairs: list[str],
                              *, kind: str) -> None:
    """把已知不可自动表达的 assert 项移入 needs_manual。

    只处理 assert 粒度，不删除整条 expect/forbid；若一条 expect 剩下空 assert，它仍可作为
    「该调用应出现」的草稿证据，但 case.raw.needs_manual 会提醒人工补回原断言。
    """
    if not isinstance(entries, list):
        return
    for i, entry in enumerate(entries):
        assert_map = entry.get("assert") if isinstance(entry, dict) else None
        if not isinstance(assert_map, dict):
            continue
        for key in list(assert_map):
            spec = assert_map[key]
            reason: str | None = None
            if _var_only_assert_key(str(key)):
                reason = f"{kind}[{i}].assert[{key}] 把变量写成 path，变量只能出现在 value/fixture"
            else:
                try:
                    api_oracle.static_check_assert_key(str(key))
                    op = api_oracle._spec_op(spec)
                    if op in api_oracle.CROSS_ELEMENT_OPS:
                        api_oracle._static_check_cross_op(f"{kind}[{i}].assert[{key}]", op, spec)
                except ValueError as e:
                    if _manual_static_issue(str(key), e):
                        reason = str(e)
                    else:
                        continue
            if reason is None:
                continue
            removed = assert_map.pop(key)
            needs_manual.append(
                "api_expectations 静态闸转人工："
                + json.dumps({"kind": kind, "index": i, "path": key, "spec": removed, "reason": reason},
                             ensure_ascii=False, sort_keys=True)
            )
            repairs.append("static_assert_downgraded_needs_manual")


def _collect_vars(obj: Any) -> set[str]:
    return set(api_oracle._VAR_RE.findall(obj)) if isinstance(obj, str) else (
        {v for x in obj for v in _collect_vars(x)} if isinstance(obj, list) else
        {v for x in obj.values() for v in _collect_vars(x)} if isinstance(obj, dict) else set())


def _append_fixture_block(path: Path, case_id: str, fixtures: dict[str, Any]) -> None:
    """把 {var:value} 以 `CASE_ID:` 块追加到 api_fixtures.yaml 尾（保留既有注释；case_id 撞库 fail-closed）。"""
    assert_assets_writable("append API fixture asset", path=path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = yaml.safe_load(text) or {} if text.strip() else {}
    if case_id in (existing or {}):
        raise ValueError(f"api_fixtures 已存在 {case_id}，拒绝覆盖")
    block = yaml.safe_dump({case_id: fixtures}, allow_unicode=True, sort_keys=False, width=100)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + block, encoding="utf-8")


def _seed_dry_run_assets(tmp_dir: Path, app: str) -> tuple[Path, Path, Path]:
    """建一组三份临时空资产，供批量 dry-run 逐行落库但不触碰真实 app 资产。"""
    cases_path = tmp_dir / "cases.yaml"
    cases_path.write_text(yaml.safe_dump(
        {"meta": {"title": f"{app} material dry-run", "schema_version": 2, "total": 0, "next_seq": 1},
         "cases": []},
        allow_unicode=True, sort_keys=False,
    ), encoding="utf-8")
    expectations_path = tmp_dir / "api_expectations.yaml"
    expectations_path.write_text(yaml.safe_dump(
        {"meta": {"schema_version": 1, "app": app, "total": 0, "next_seq": 1},
         "expectations": []},
        allow_unicode=True, sort_keys=False,
    ), encoding="utf-8")
    fixtures_path = tmp_dir / "api_fixtures.yaml"
    fixtures_path.write_text("# dry-run only\n", encoding="utf-8")
    return cases_path, expectations_path, fixtures_path


def summarize_convert_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐行 convert_row 结果压成 P4 决策需要的分箱统计。

    ``needs_operation`` 在此处是 readiness 缺口分类，不是 Case 生命周期值。
    对历史转换结果仍兼容同名 status，新产物的 status 恒为 draft。
    """
    buckets = Counter({
        "auto_draft": 0,
        "needs_operation": 0,
        "needs_manual": 0,
        "needs_operation_and_manual": 0,
        "failed": 0,
    })
    statuses: Counter[str] = Counter()
    missing_operations: Counter[str] = Counter()
    manual_reasons: Counter[str] = Counter()
    error_reasons: Counter[str] = Counter()
    repair_counter: Counter[str] = Counter()
    failed_samples: list[dict[str, Any]] = []

    for result in results:
        if not result.get("ok"):
            buckets["failed"] += 1
            err = str(result.get("error") or "unknown")
            error_reasons[err] += 1
            if len(failed_samples) < 10:
                failed_samples.append({
                    "row": result.get("row"),
                    "external_id": result.get("external_id"),
                    "item": result.get("item"),
                    "error": err,
                })
            continue

        status = str(result.get("status") or "draft")
        statuses[status] += 1
        missing_ops = [str(op) for op in (result.get("missing_operations") or [])]
        needs_manual = [str(reason) for reason in (result.get("needs_manual") or [])]
        missing_operations.update(missing_ops)
        manual_reasons.update(needs_manual)
        repair_counter.update(str(r) for r in (result.get("repairs") or []))

        has_missing_ops = bool(missing_ops) or status == "needs_operation"
        has_manual = bool(needs_manual)
        if has_missing_ops and has_manual:
            buckets["needs_operation_and_manual"] += 1
        elif has_missing_ops:
            buckets["needs_operation"] += 1
        elif has_manual:
            buckets["needs_manual"] += 1
        else:
            buckets["auto_draft"] += 1

    flags = {
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": buckets["failed"],
        "needs_operation": buckets["needs_operation"] + buckets["needs_operation_and_manual"],
        "needs_manual": buckets["needs_manual"] + buckets["needs_operation_and_manual"],
    }
    return {
        "buckets": dict(buckets),
        "flags": flags,
        "statuses": dict(statuses),
        "missing_operations": dict(missing_operations.most_common()),
        "suite_profile": suite_profile_from_convert_results(results),
        "needs_manual_reasons": dict(manual_reasons.most_common(20)),
        "repairs": dict(repair_counter.most_common()),
        "errors": dict(error_reasons.most_common(20)),
        "failed_samples": failed_samples,
    }


def dry_run_convert_material(
    app: str,
    indexed_rows: list[tuple[int, dict[str, Any]]],
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 300.0,
    retries: int = 1,
    runner: CommandRunner = _subprocess_runner,
    note: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """逐行降解到临时资产，只返回分箱统计；真实 cases/expectations/fixtures 零写入。"""
    started_at = datetime.now().astimezone()
    started_mono = time.monotonic()
    clusters_by_row = _cluster_indexed_rows(indexed_rows)
    with tempfile.TemporaryDirectory(prefix="autophone-convert-material-") as d:
        cases_path, expectations_path, fixtures_path = _seed_dry_run_assets(Path(d), app)
        results: list[dict[str, Any]] = []
        total = len(indexed_rows)
        for ordinal, (row_index, row) in enumerate(indexed_rows, start=1):
            row_started_mono = time.monotonic()
            norm = row if "item" in row and "assertion" in row else _norm_row(row)
            meta = {
                "ordinal": ordinal,
                "total": total,
                "row": row_index,
                "external_id": norm.get("external_id") or None,
                "item": norm.get("item") or None,
            }
            if progress:
                progress({"event": "start", **meta})
            out = convert_row(
                app, norm, provider=provider, model=model, timeout_s=timeout_s,
                retries=retries, runner=runner,
                cases_path=cases_path, api_expectations_path=expectations_path,
                api_fixtures_path=fixtures_path, note=note,
                cluster_context=clusters_by_row.get(row_index),
            )
            row_duration_s = round(time.monotonic() - row_started_mono, 3)
            out = {
                **out,
                "row": row_index,
                "external_id": norm.get("external_id") or None,
                "item": norm.get("item") or None,
                "cluster_key": list(_material_cluster_key(norm)),
                "cluster_size": len(clusters_by_row.get(row_index) or []),
                "duration_s": row_duration_s,
            }
            results.append(out)
            if progress:
                progress({
                    "event": "done",
                    **meta,
                    "ok": bool(out.get("ok")),
                    "status": out.get("status"),
                    "missing_operations": out.get("missing_operations") or [],
                    "needs_manual": out.get("needs_manual") or [],
                    "error": out.get("error"),
                    "duration_s": row_duration_s,
                })

    summary = summarize_convert_results(results)
    ended_at = datetime.now().astimezone()
    duration_s = round(time.monotonic() - started_mono, 3)
    return {
        "ok": summary["flags"]["failed"] == 0,
        "dry_run": True,
        "app": app,
        "total": len(indexed_rows),
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": ended_at.isoformat(timespec="seconds"),
        "duration_s": duration_s,
        "average_row_duration_s": round(duration_s / len(indexed_rows), 3) if indexed_rows else 0.0,
        **summary,
    }


def convert_row(
    app: str,
    row: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 300.0,
    retries: int = 1,
    runner: CommandRunner = _subprocess_runner,
    cases_path: Path | None = None,
    api_expectations_path: Path | None = None,
    api_fixtures_path: Path | None = None,
    note: str | None = None,
    cluster_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把一行集成用例降解成三份草稿并落库（全 draft）。row 可是原始 xlsx 行或已归一行。

    返回：
      - {ok:True, case_id, status, case, api_expectations, fixtures, needs_manual,
         missing_operations, repairs, paths}                     成功落三份草稿
      - {ok:False, error, attempts}                              fail-closed（无回包/不贴 schema/不过静态闸/缺口）
    """
    profile = load_app_profile(app)
    norm = row if "item" in row and "assertion" in row else _norm_row(row)
    if not norm.get("item"):
        return {"ok": False, "error": "行缺少用例场景名称（item）"}

    req = AiRequest(
        prompt=_build_prompt(
            norm,
            _capability_brief(app, root=getattr(profile, "root", None) or APP_ASSET_ROOT),
            cluster_context=cluster_context,
        ),
        system=_SYSTEM, tools="none", json_schema=_CONVERT_SCHEMA,
        provider=provider, model=model, timeout_s=timeout_s,
    )

    # 回包获取带重试：超时/截断 JSON/schema 漂移是瞬态（同类行普遍能过），单行瞬态不废整批。
    # 静态闸失败**不**重试——那是内容问题，重打大概率原样再来，留给闸门 fail-closed。
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(_CONVERT_SCHEMA)
    attempts = max(1, retries + 1)
    payload: dict[str, Any] | None = None
    repairs: list[str] = []
    error = "convert-api agent returned no structured json"
    res = None
    for _ in range(attempts):
        res = invoke(req, runner=runner)
        if not res.ok or not isinstance(res.json, dict):
            error = res.error or "convert-api agent returned no structured json"
            continue
        candidate = res.json
        coerce_repairs = _coerce_nullables(candidate)
        schema_errors = sorted(validator.iter_errors(candidate), key=lambda e: list(e.path))
        if schema_errors:
            detail = "；".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
                               for e in schema_errors[:5])
            error = f"convert-api 回包不贴 schema（拒绝写脏）：{detail}"
            continue
        payload = candidate
        repairs.extend(coerce_repairs)
        break
    if payload is None or res is None:
        return {"ok": False, "error": error, "attempts": attempts}

    case = payload["case"]
    exps = payload.get("api_expectations") or {}
    fixtures = payload.get("fixtures") or {}
    needs_manual = list(payload.get("needs_manual") or [])
    expect = exps.get("expect") or []
    forbid = exps.get("forbid") or []

    # 静态闸 ①：case.steps 不得含 assert_*（断言不下沉 recipe step，§五）。AI 违规时确定性剥离：
    # 断言步原文进 needs_manual（人工核对是否已被 api_expectations 覆盖），只在剥完没有可执行步时拒收。
    body, stripped = _strip_assert_steps(case["steps"])
    if stripped:
        if not body:
            return {"ok": False, "attempts": attempts,
                    "error": "case.steps 全为 assert_*（剥离后无可执行步骤），拒绝写脏"}
        case["steps"] = body
        needs_manual.extend(
            "steps 中剥离的 assert 步（核对是否已被 api_expectations 覆盖）："
            + json.dumps(s, ensure_ascii=False, sort_keys=True)
            for s in stripped)
        repairs.append("assert_steps_stripped")

    # 确定性形态修复：assert 键漏写 resp. 前缀的补齐（目标就是响应体），修完仍过静态闸。
    if _prefix_resp_keys(expect) + _prefix_resp_keys(forbid):
        repairs.append("assert_keys_prefixed_resp")

    # P4a fail-soft：实证出来的半截跨元素/非法变量 path/裸谓词断言不写成脏 assert，
    # 摘入 needs_manual；其它未知 JSONPath 错误仍由下面的静态闸 fail-closed。
    _downgrade_manual_asserts(expect, needs_manual, repairs, kind="expect")
    _downgrade_manual_asserts(forbid, needs_manual, repairs, kind="forbid")

    # 静态闸 ②：api_expectations 的 JSONPath / op 过求值器静态校验（§六）。
    try:
        api_oracle.static_validate_expect_entries(expect, kind="expect")
        api_oracle.static_validate_expect_entries(forbid, kind="forbid")
    except ValueError as e:
        return {"ok": False, "attempts": attempts,
                "error": f"api_expectations 不过 JSONPath 静态闸（拒绝写脏）：{e}"}

    # 静态闸 ③：断言里每个 ${var} 都要在 fixtures 有键（值可 null），否则外部真值缺口不浮现。
    referenced = _collect_vars(expect) | _collect_vars(forbid)
    missing_vars = sorted(v for v in referenced if v not in fixtures)
    if missing_vars:
        return {"ok": False, "attempts": attempts,
                "error": f"断言引用的 ${{var}} 未在 fixtures 声明：{missing_vars}（fail-closed）"}

    # 缺操作核对（不轻信 AI）：referenced operation 里不在真实注册表的
    # 保留为 readiness 缺口，但不创造新的生命周期状态。
    referenced_ops = _referenced_operations(case["steps"])
    available = _available_operations(
        app,
        root=getattr(profile, "root", None) or APP_ASSET_ROOT,
    )
    contract_blockers = _catalog_contract_blockers(app, case["steps"])
    missing_ops = sorted({_canonicalize_missing_op(op) for op in referenced_ops
                          if op not in available or op in contract_blockers})
    isolated_order_payment = requires_order_payment_isolation(norm)
    if isolated_order_payment:
        # 不是待实现的普通 operation：这是 profile 的不可逾越边界。
        # 即使模型遗漏 pay/submit step，仍以结构化缺口隔离，不进执行面。
        missing_ops = sorted(set(missing_ops) | {"order_payment_isolation"})
        needs_manual.append("来源包含支付或提交订单路径；按 App profile 隔离，需人工治理")
    case_status = "draft"
    hints = build_role_hints(
        case["steps"], {"expect": expect, "forbid": forbid}, app=app, row=norm,
    )
    setup_steps, body_steps = _split_setup_prefix(case["steps"], hints.get("shareable_prefix") or [])

    # ── 落三份草稿（全 draft）。case 先落拿到自分配主键 R{seq}，断言/真值按同键挂。──
    cpath = cases_path or profile.cases_path()
    prov_note = f"AI convert-api via {res.provider}" + (f"/{res.model}" if res.model else "")
    if note:
        prov_note += f"; {note}"
    raw = {"external_id": norm.get("external_id") or None,
           "source_ref": norm.get("source_ref") or None,
           "source_steps": norm.get("source_steps") or None,
           "business_rule": norm.get("business_rule") or None,
           "source_assertion": norm.get("assertion") or None,
           "hints": hints}
    if needs_manual:
        raw["needs_manual"] = needs_manual
    execution_blockers = [f"缺少可执行 Operation：{op}" for op in missing_ops]
    if contract_blockers:
        execution_blockers.extend(
            "cart catalog fixture missing or unapproved for " + op
            for op in sorted(contract_blockers)
        )
    if isolated_order_payment:
        execution_blockers.append(
            "order/payment source path is isolated: profile forbids payment and real order submission"
        )
    if execution_blockers:
        raw["execution_blockers"] = execution_blockers
    case_fields = {
        "module": case["module"], "item": case["item"],
        "automation": "",
        "automation_reason": (
            "自动化条件未满足：缺少可执行 Operation（" + "、".join(missing_ops) + "）"
            if missing_ops else "自动化状态待人工评估"
        ),
        "steps": body_steps,
        "raw": {k: v for k, v in raw.items() if v is not None},
        "provenance": {"source": "conversation", "from": f"material:{norm.get('external_id') or norm.get('source_ref') or norm['item']}",
                       "note": prov_note},
    }
    order_hints = infer_order_hints(app, case["steps"], norm, {"expect": expect, "forbid": forbid})
    if order_hints.get("order_consumes") is False:
        case_fields["order_consumes"] = False
    if setup_steps:
        case_fields["setup"] = setup_steps
    new_case = cases_store.append_case(cpath, case_fields, status=case_status)
    case_id = str(new_case["id"])

    paths: dict[str, str] = {"case": str(cpath)}
    if expect or forbid:
        epath = api_expectations_path or profile.api_expectations_path()
        exp_fields: dict[str, Any] = {"title": case["item"],
                                      "provenance": {"source": "conversation", "note": prov_note}}
        if expect:
            exp_fields["expect"] = expect
        if forbid:
            exp_fields["forbid"] = forbid
        with asset_store.path_write_lock(epath):
            asset_store.append_item(
                API_EXPECTATIONS_SPEC,
                epath,
                exp_fields,
                status="draft",
                item_id=case_id,
            )
        paths["api_expectations"] = str(epath)
    if fixtures:
        fpath = api_fixtures_path or profile.api_fixtures_path()
        _append_fixture_block(fpath, case_id, fixtures)
        paths["fixtures"] = str(fpath)

    return {
        "ok": True, "case_id": case_id, "status": case_status,
        "case": new_case, "api_expectations": {"expect": expect, "forbid": forbid},
        "fixtures": fixtures, "needs_manual": needs_manual, "hints": hints,
        "missing_operations": missing_ops, "repairs": repairs, "paths": paths,
    }
