"""lift.py — M1: 把 record_capture 的 trace 提升成语义 typed IR。

录制/回放设计见 docs/record-replay-design.md §四/§八。M0（tools/phase1/record_capture.py）
产出的是逐点 `tap_xy(px,py)` 的 dry-run IR——可回放但无语义、对布局漂移脆弱。M1 在**创建期**
把每个 tap 提升成 `tap_text("确认")`：喂那一刻的 before_shot + 落点坐标给本地 agent，问落点上
是什么文字。判得准（high/medium 置信）→ `tap_text(query)`；判不准 / 纯图标 / AI 出错 → **回退
保留 `tap_xy(px,py)`**（有损但不杜撰、不二次放大）。

**落点附近 OCR 校验（防 AI 读错红圈对应的控件 + 同文字消歧）**：AI 给出候选文字后，不直接采信，
而是在**原始 before_shot**（非画了红圈的那张）上跑 OCR，要求落点附近 `NEAR_RADIUS_PX` 内真有一个
含该文字的 box，才认这次提升；OCR 里找不到该文字、或只在远处出现 → 视作 AI 误读，**回退 tap_xy**。
当该文字在整页出现 ≥2 次（真歧义，如多张商品卡都有「支付」）时，按落点收紧一个归一化 `region`
写进 step（`tap_text(query, region=[nx0,ny0,nx1,ny1])`），让回放只在落点邻域选框、不被远处同名文案
带偏；唯一命中则不收紧（最耐布局漂移）。OCR 跑不了（二进制未编 / 图损坏）只**降级到纯 AI 提升**
（reason=lifted_no_ocr），不把缺 OCR 变成全量回退坐标——校验是增强，缺位不致命。

M3 在同一 schema 上多了 type/swipe 事件。它们**无语义可猜**——type 是字面文本、swipe 是几何手势，
没有「落点上是什么文字」这种待识别的东西。所以二者**原样直通**（type→`type_text(text)`、
swipe→`swipe_xy(...)`），不喂模型；只有 tap 走 AI 提升。最终 step 列表照样过 validate_recipe 兜底。

铁律：
  · 严格创建期（roadmap §8.4）——本模块调 autophone.application.ai，**运行期回归链严禁触达**。
  · 提升是 best-effort 增强，单个 tap 判不准只回退该步，绝不让整条 trace 失败；
    但最终 step 列表**必须过 recipe.validate_recipe**（fail-closed），产不出可回放 recipe 就抛。
  · 坐标只用 preview——回退/直通喂 preview，绝不把 trace 里的 native 塞回去（见 §三坐标契约）。
  · 主动事件须有 uds_result.ok=True；原生指针须通过观测字段校验，仅输出坐标 draft IR。

落 autophone/record/（不在 tools/）因为依赖 autophone.application.ai——既定层级方向是
autophone.record → autophone.application，与 ai_draft 的消费方向一致。AI 调用是可注入
seam（`invoker`），宿主侧决策逻辑无需真模型/真 VM 即可单测（`--selftest`）。

归位（BL-014 执行段第二轮，2026-09-10）：BL-012 时因模块级 `platform.ai` 与函数体内
三处 `platform.ai_draft` 留守 platform；两者已随 BL-014 表 §4.1 裁定归位
`autophone/application/`，本模块的 platform 依赖归零，遂由 `platform/record_lift.py`
搬到本路径。
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from autophone.assets import screens as screen_assets
from autophone.paths import APP_ASSET_ROOT
from autophone.assets.policy import assert_assets_writable
from autophone.application.ai import AiRequest, AiResult, invoke
from autophone.device.uds import EXPECTED_PREVIEW, HARDWARE_KEYS
from autophone.device.ocr_target import (DEFAULT_MIN_CONFIDENCE, Match, OcrError,
                                     find_text, run_ocr)
from autophone.execution.engine.recipe import RecipeError, operation_scope, operations_for_app, validate_recipe

# AI 调用 seam：(AiRequest) -> AiResult。默认真调本地 agent；selftest 注入假的。
Invoker = Callable[[AiRequest], AiResult]
# OCR 校验 seam：(图片路径) -> {"image_w","image_h","boxes":[...]}。默认真跑 Swift ocr_locate；
# selftest 注入假的（无 VM/无二进制）。与 Invoker 同款可注入。
OcrRunner = Callable[[Path], dict[str, Any]]

# 默认接受的置信档；low 一律回退 tap_xy。
DEFAULT_ACCEPT = ("high", "medium")
# 文字标签的合理上限——超过当成模型在描述而非读标签，回退。
MAX_LABEL_LEN = 40
# 「落点附近 OCR 命中」判定半径（preview 像素）：AI 给的文字必须在落点这么近内有一个同文字
# OCR box 才认提升；否则视作 AI 读错红圈对应的控件，回退 tap_xy。
NEAR_RADIUS_PX = 60.0
# 同文字出现 ≥2 次（真歧义）时写进 step 的 region 框半宽（preview 像素）：比命中半径略大，
# 给回放期布局微漂留容差，又足以排除远处的同名 box。唯一命中不收紧 region（最耐漂移）。
REGION_HALF_PX = 90.0
# element_type 驱动的 region 策略（A+D）：AI 除了读文字，还判元素类型；「有定位」的这些类型在回放期
# **必须锚 region**——录制期 OCR 唯一命中不代表回放期唯一。右上角「预约」这种小按钮，不锚 region 的话
# 回放的子串匹配会飘到正文里的同名文字（「可预约」「预约中」）而点错，就是「对不上」的根因。
POSITIONED_TYPES = frozenset({"button", "overlay", "dialog", "nav_top", "tab"})
# 有定位类型放宽 OCR 紧邻半径：右上小字按钮的 tap 点常与 OCR 框中心差 >60px，默认半径会误判 far→退坐标。
BUTTON_NEAR_RADIUS_PX = 100.0
# 顶栏 / 底栏带（归一化 y）：落点落在这两条带里的有定位按钮，锚成「窄 y 带 + 落点横向邻域」——
# 既排除正文里的同名文字（y 差出带外），又对左右微漂留容差。带外的有定位按钮回落到落点邻域方框。
TOP_BAND_NY = 0.14
BOTTOM_BAND_NY = 0.10
BAND_REGION_HALF_PX = 110.0
# 场景①「整行可点、落点落在行内空白」：落点 NEAR_RADIUS_PX 紧邻找不到同文字时，退一步在
# 「同一水平行带」内找——纵向收紧到 ±ROW_BAND_HALF_PY（一行高量级，隔开上下相邻行），横向不限
# （整行可点，label 可能在行首而手指在行尾，距离能跨大半屏）。命中即认这是被点行的 label，写一个
# 「全宽 x、窄 y 行带」region：回放只在该行带内按文字串选框，既不被别处同名带偏、又不要求手指落在
# 文字正下方。靠 tap_text 的文字串匹配在带内挑出 label box，行带里的其它文案（勾选标记等）不干扰。
ROW_BAND_HALF_PY = 28.0
# 无定位时的门店选择页（「选择餐厅 / 常去&收藏」）是一整行可点；落点常在店铺卡片空白或副文案上。
# 用 OCR 从落点所在行提取店名，回放时仍走 tap_text + 行带 region，避免裸 tap_xy 受列表排序/定位状态影响。
STORE_ROW_MATCH_HALF_PY = 72.0
STORE_ROW_REGION_HALF_PY = 45.0
ADDRESS_ROW_MIN_NY = 0.46
ADDRESS_ROW_MAX_NY = 0.92
ADDRESS_ROW_MATCH_HALF_PY = 72.0
BOTTOM_CTA_MIN_NY = 0.78
BOTTOM_CTA_REGION_HALF_PX = 90.0
BOTTOM_CTA_REGION_HALF_PY = 56.0
BOTTOM_CTA_TAP_MAX_DY_PX = 64.0
BOTTOM_CTA_QUERIES = ("加入购物车", "去支付", "去结算", "立即购买", "选好了")
PRODUCT_CARD_MATCH_HALF_PY = 92.0
PRODUCT_CARD_REGION_TOP_PX = 72.0
PRODUCT_CARD_REGION_BOTTOM_PX = 138.0
PRODUCT_CARD_MIN_NX = 0.20
PRODUCT_CARD_TITLE_HINTS = (
    "披萨", "套餐", "牛肉", "榴莲", "汉堡", "意面", "饭", "小食", "鸡", "虾", "芝士", "薯", "饮", "可乐",
)
PRODUCT_CARD_EXCLUDES = (
    "全部", "新品", "折扣", "热", "预约中", "选择餐厅", "外带自取", "选好了", "优惠中心", "卡券",
    "超值推荐", "一人食", "多人餐", "新手拍", "尊享卡特惠", "皇冠", "卷边披萨", "薄底披萨", "现做汉堡",
    "面/饭", "面\\/饭", "披萨热销", "热销第", "开卡享", "补货中", "¥",
)
# 场景②「纯图标无文字」（如左上角关闭 X）：AI 读不出文字时，若判得出是常见语义图标，提升成
# tap_ax(a11y label)——结构定位够得着 OCR 够不着的图标。a11y label 是按图标语义猜的（创建期没有活
# AX 树可核），所以两道护栏把风险夹死：① region 限定落点邻域，AX 只在附近选节点、不被别处同名带偏；
# ② fallback_xy 填录制原坐标，AX 没命中 / 无 provider 时退化成今天的 tap_xy——猜错也零回归。
ICON_A11Y_LABELS: dict[str, str] = {
    "close": "关闭", "back": "返回", "search": "搜索", "cart": "购物车",
    "share": "分享", "more": "更多", "add": "添加", "menu": "菜单",
}
# 喂 AI 的「落点放大裁图」窗口（preview 像素，与录制卡片展示同尺寸 200×140，覆盖一个卡片/按钮
# 量级）+ 放大倍数：裁出落点附近一小块再放大，小字/密集控件的文字识别更准。整图仍一并喂（保上下文）。
CROP_PW, CROP_PH = 200, 140
CROP_UPSCALE = 3
# 提升产物形态变了就 +1：旧缓存会被 get_lift_preview_cache 判过期、重新提升。
# 14 = 加 step_events 映射 + 规则命中改记 source="heuristic"（旧缓存记的是 "ai"）。
LIFT_PREVIEW_VERSION = 14


class LiftError(RuntimeError):
    """提升产不出可回放 recipe（非 tap 事件 / 最终 step 列表过不了 validate_recipe）。"""


class AlignmentError(RuntimeError):
    """Script alignment sidecar failed its host-side structural contract."""


_ALIGNMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["script_step", "event_indices", "verdict", "confidence", "note"],
                "properties": {
                    "script_step": {"type": ["string", "null"]},
                    "event_indices": {"type": "array", "items": {"type": "integer"}},
                    "verdict": {"enum": ["matched", "missing", "extra"]},
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

_ALIGNMENT_SYSTEM = (
    "你是录制动作与用例剧本的序列对齐员。输入是一组 script steps 和一组已经提升过的录制 decisions。"
    "输出 groups：每个 script step 必须出现一次，verdict 为 matched 或 missing；录制里不属于剧本的动作"
    "用 script_step=null、verdict=extra 表示。宁可 missing/extra，也不要硬凑。动作下标不能越界、不能重复，"
    "matched 分组必须保持剧本顺序和动作顺序。"
)


def _script_steps(script: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(script, dict):
        return []
    rows = script.get("steps")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        sid = str(row.get("id") or f"s{i}").strip() or f"s{i}"
        expect = str(row.get("expect") or "").strip()
        out.append({"id": sid, "text": text, "expect": expect})
    return out


def _decision_alignment_summary(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pos, d in enumerate(decisions):
        step = d.get("step") if isinstance(d.get("step"), dict) else {}
        item: dict[str, Any] = {
            "index": pos,
            "trace_index": d.get("index"),
            "source": d.get("source"),
            "reason": d.get("reason"),
            "step": step,
        }
        for key in ("ai_text", "element_type"):
            if d.get(key):
                item[key] = d.get(key)
        if d.get("screen_id"):
            item["screen_id"] = d.get("screen_id")
        rows.append(item)
    return rows


def _validate_script_alignment(
    candidate: dict[str, Any],
    *,
    script_steps: list[dict[str, str]],
    decision_count: int,
    script_source: str,
) -> dict[str, Any]:
    groups = candidate.get("groups")
    if not isinstance(groups, list):
        raise AlignmentError("alignment groups must be a list")
    step_ids = [s["id"] for s in script_steps]
    step_order = {sid: i for i, sid in enumerate(step_ids)}
    seen_steps: set[str] = set()
    seen_events: set[int] = set()
    normalized: list[dict[str, Any]] = []
    last_script_order = -1
    last_event = -1
    for raw in groups:
        if not isinstance(raw, dict):
            raise AlignmentError("alignment group must be an object")
        verdict = raw.get("verdict")
        script_step = raw.get("script_step")
        idxs = raw.get("event_indices")
        if verdict not in {"matched", "missing", "extra"}:
            raise AlignmentError(f"invalid alignment verdict: {verdict!r}")
        if not isinstance(idxs, list) or any(not isinstance(i, int) or isinstance(i, bool) for i in idxs):
            raise AlignmentError("event_indices must be an integer array")
        if any(i < 0 or i >= decision_count for i in idxs):
            raise AlignmentError("event index out of range")
        if any(i in seen_events for i in idxs):
            raise AlignmentError("event index appears in more than one group")
        if verdict == "extra":
            if script_step is not None or not idxs:
                raise AlignmentError("extra groups require script_step=null and non-empty event_indices")
        elif verdict == "missing":
            if not isinstance(script_step, str) or script_step not in step_order or idxs:
                raise AlignmentError("missing groups require a known script_step and empty event_indices")
        else:
            if not isinstance(script_step, str) or script_step not in step_order or not idxs:
                raise AlignmentError("matched groups require a known script_step and non-empty event_indices")
            order = step_order[script_step]
            if order < last_script_order:
                raise AlignmentError("matched groups are not in script order")
            if idxs and min(idxs) <= last_event:
                raise AlignmentError("matched group event_indices are not monotonic")
            last_script_order = order
            last_event = max(idxs)
        if isinstance(script_step, str):
            if script_step in seen_steps:
                raise AlignmentError(f"script step appears more than once: {script_step}")
            seen_steps.add(script_step)
        seen_events.update(idxs)
        normalized.append({
            "script_step": script_step,
            "event_indices": idxs,
            "verdict": verdict,
            "confidence": raw.get("confidence") if raw.get("confidence") in {"high", "medium", "low"} else "low",
            "note": str(raw.get("note") or "")[:240],
        })
    for step in step_ids:
        if step not in seen_steps:
            normalized.append({
                "script_step": step,
                "event_indices": [],
                "verdict": "missing",
                "confidence": "high",
                "note": "host_backfill",
            })
    for idx in range(decision_count):
        if idx not in seen_events:
            normalized.append({
                "script_step": None,
                "event_indices": [idx],
                "verdict": "extra",
                "confidence": "high",
                "note": "host_backfill",
            })
    return {"align_version": 1, "script_source": script_source, "groups": normalized}


def script_source_label(script: dict[str, Any] | None) -> str:
    """剧本快照的来源标签：``freeform`` / ``case:R123@rev4`` / ``case:R123@rev4+edited``。

    ``+edited`` 表示审查时改过用例文案；用例绑定与原始用例步骤仍保留在快照里
    （``case_steps``），标签只负责让 alignment 与 provenance note 不丢来源。
    """
    if not isinstance(script, dict) or script.get("source") != "case":
        return "freeform"
    revision = script.get("case_revision")
    label = f"case:{script.get('case_id') or ''}" + (
        f"@rev{revision}" if revision not in (None, "") else ""
    )
    return label + ("+edited" if script.get("edited") else "")


def validate_script_alignment(
    candidate: dict[str, Any],
    *,
    script: dict[str, Any],
    decision_count: int,
) -> dict[str, Any]:
    """Validate a user-reviewed alignment against the current script snapshot.

    AI output and browser-edited output share the same fail-closed host
    contract so a stale or out-of-order mapping can never be persisted.
    """
    steps = _script_steps(script)
    if not steps:
        raise AlignmentError("script has no steps")
    return _validate_script_alignment(
        candidate,
        script_steps=steps,
        decision_count=decision_count,
        script_source=script_source_label(script),
    )


def _align_script(
    script: dict[str, Any] | None,
    decisions: list[dict[str, Any]],
    *,
    invoker: Invoker,
    provider: str | None,
    model: str | None,
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    steps = _script_steps(script)
    if not steps:
        return None, None
    src = script_source_label(script)
    # 一句说明通常就是对整段录制的概括（例如“点击自取，加购商品，进入确认页”）。
    # 这种 1:N 关联没有歧义，不必让模型生成容易出现空 script_step/event_indices 的结构。
    if len(steps) == 1:
        event_indices = list(range(len(decisions)))
        verdict = "matched" if event_indices else "missing"
        return _validate_script_alignment(
            {
                "groups": [{
                    "script_step": steps[0]["id"],
                    "event_indices": event_indices,
                    "verdict": verdict,
                    "confidence": "high",
                    "note": "single_script_step",
                }]
            },
            script_steps=steps,
            decision_count=len(decisions),
            script_source=src,
        ), None
    prompt = (
        "script steps:\n"
        + json.dumps(steps, ensure_ascii=False, indent=2)
        + "\n\ndecisions:\n"
        + json.dumps(_decision_alignment_summary(decisions), ensure_ascii=False, indent=2)
    )
    for attempt in range(2):
        try:
            res = invoker(AiRequest(
                prompt=prompt,
                system=_ALIGNMENT_SYSTEM,
                tools="none",
                json_schema=_ALIGNMENT_SCHEMA,
                provider=provider,
                model=model,
                timeout_s=timeout_s,
            ))
            if not res.ok:
                return None, f"ai_error: {res.error}"
            return _validate_script_alignment(
                res.json or {},
                script_steps=steps,
                decision_count=len(decisions),
                script_source=src,
            ), None
        except AlignmentError as exc:
            if attempt == 1:
                return None, f"invalid_alignment: {exc}"
        except Exception as exc:  # noqa: BLE001 - alignment is fail-open metadata
            return None, f"{type(exc).__name__}: {exc}"
    return None, "invalid_alignment"


def _ensure_app_operations(
    app: str | None,
    *,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Callable[..., Any]]:
    """Best-effort app operation import for lifter-created operation steps."""
    app = str(app or "").strip()
    if not app:
        return {}
    try:
        from autophone.execution.app_recipes import import_app_recipes

        import_app_recipes(app, root=root)
    except Exception:  # noqa: BLE001 - missing app recipe support should not block action-only traces
        return {}
    return operations_for_app(app, root=root)


# 喂模型的「落点文字识别」结果 schema——被点元素自身的可见文字 + 置信档；text 为 null（纯图标）时
# 额外给一个 icon 语义（闭集，供 ② 提升成 tap_ax）。
_LIFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # Codex strict structured-output requires every declared property to be
    # present; semantic optionality is represented by the existing null enums.
    "required": ["text", "confidence", "icon", "element_type"],
    "properties": {
        "text": {"type": ["string", "null"],
                 "description": "被点 UI 元素的可见文字标签；纯图标/头像/无文字控件则 null"},
        "confidence": {"enum": ["high", "medium", "low"],
                       "description": "high=清晰命中带文字的控件；medium=较可能；low=猜测/落点附近文字含糊"},
        "icon": {"enum": [*ICON_A11Y_LABELS, None],
                 "description": "仅当 text 为 null（纯图标）时填：该图标若是这些常见语义控件之一就给其语义，"
                                "否则 null。close=关闭/X、back=返回/＜、search=搜索放大镜、cart=购物车、"
                                "share=分享、more=更多/···、add=加号、menu=菜单/三横"},
        "element_type": {
            "enum": ["button", "overlay", "dialog", "list_row", "tab", "nav_top",
                     "text_link", "other", None],
            "description": "被点元素的类型（拿不准给 null）：button=独立按钮；overlay=悬浮/浮层上的控件"
                           "（如页面右上角悬浮的入口/角标）；dialog=弹窗/模态框里的按钮；list_row=整行"
                           "可点的列表项；tab=底部/顶部标签栏项；nav_top=顶部导航栏里的按钮（返回旁/右上角）；"
                           "text_link=正文里的文字链接；other=其它。",
        },
    },
}

_SYSTEM = (
    "你在把一次 iOS 触摸操作识别成语义标签。给你一张 430×932 的 iOS 截图和一个点击落点（preview 坐标，"
    "原点在左上角）。判断该落点**所在的可点击元素**（按钮 / 列表行 / 单元格）显示的**可见文字**，"
    "返回该元素自身的文字标签。\n"
    "落点可能落在该元素内部的空白处——典型如一整行可点的列表项，文字在行首而手指点在行尾——这时仍返回"
    "该行/该元素的文字，不要因为落点正下方是空白就返回 null；但不要返回上下相邻**其它行**的文字。\n"
    "若落点所在元素是纯图标 / 头像 / 无任何文字的控件，text 返回 null，并在 icon 给出图标语义（见 schema）。\n"
    "同时判断被点元素的类型 element_type（见 schema）——尤其顶栏 / 浮层 / 弹窗里的按钮务必标出："
    "回放时要靠它把文字匹配锚定在正确区域，避免点到正文里同名的文字。\n"
    "confidence：high=能清楚判断被点元素及其文字/图标语义；medium=较可能但不完全确定；low=猜测，或落点"
    "附近文字含糊/多义。宁可 null/low 也不要编造一个并不属于被点元素的文字。"
)


@dataclass
class LiftDecision:
    """逐事件的提升判定（留痕：为什么提升成 tap_text、为什么回退、或为什么直通）。"""
    index: int
    # "ai"=模型读图判出（且过了 OCR 邻域校验）｜"heuristic"=app 规则按 OCR 版面判出，没调模型
    # ｜"fallback"=判不准，回退录制原坐标｜"passthrough"=type/swipe/key 原样直通
    # ｜"semantic"=抓包语义旁路（linkId 之类）。审查页据此显示证据强度——OCR 规则不许冒充模型高置信。
    source: str
    reason: str              # lifted / no_text / low_confidence(...) / text_too_long / ai_error: ... / type / swipe
    step: dict[str, Any]     # 落地的 typed IR step
    confidence: str | None = None
    ai_text: str | None = None
    # AI 判出的元素类型（button/overlay/dialog/list_row/tab/nav_top/text_link/other），驱动回放 region
    # 策略；「有定位」类型（POSITIONED_TYPES）会强制锚 region。留痕供审查 UI 展示 + 复盘。
    element_type: str | None = None
    # 落库前逐步审查 UI 用：每个 tap 的落点（preview px,py）+ 那一刻的 before_shot 文件名，
    # 让审查者无论提升成功与否都能在原图上画红圈、人工决定保留坐标还是改文字。lift_trace 统一回填。
    preview: list[int] | None = None
    before_shot: str | None = None
    # A semantic sidecar may collapse several brittle UI gestures into one
    # stable operation (for example category navigation + product detail + add).
    source_event_indices: list[int] | None = None
    semantic: dict[str, Any] | None = None
    # True for steps a rule *added* rather than lifted from a recorded event (a
    # conditional guard, say).  They have no human gesture behind them, so replay
    # timing must not attribute a recorded timestamp to them (§倍速回放).
    synthetic: bool = False


# ── app 专属提升规则的挂载点（版面启发式不进通用提升器） ─────────────────────
# 通用提升器只做「模型读图 + OCR 邻域校验 + 图标 AX + 回退坐标」这套与 app 无关的判断。
# 「选择餐厅页的门店行」「底部加购按钮」这类**认版面**的规则天然是某个 app 的知识，放这里
# 会随 app 增多长成谁也不敢动的 if 链，且对没这些页面的 app 白跑一遍 OCR。所以下沉到
# `apps/<app>/lift_rules.py`，由 app 自己声明 EVENT_RULES / DECISION_RULES。

@dataclass
class EventRuleContext:
    """一个 tap 事件在调模型之前的可判定材料（app 规则只吃这些）。"""
    src: Path                                  # 该事件的 before_shot
    px: int
    py: int
    ocr: OcrRunner                             # 单次 lift 内按图缓存，规则可放心多次调
    min_confidence: float
    app_operations: dict[str, Callable[..., Any]]   # 该 app 已注册的 operation（决定能不能提升成算子）


@dataclass
class EventRuleHit:
    """规则判定命中：直接给出可回放 step，跳过模型。"""
    step: dict[str, Any]
    reason: str                                # 进 decision.reason，审查页据此显示徽标
    query: str | None = None                   # 人读的命中文本（进 ai_text 列）
    element_type: str | None = None


@dataclass
class DecisionRuleContext:
    """全部事件提升完之后的整条改写材料（合并/插入守卫这类跨事件规则）。"""
    app: str
    trace: dict[str, Any]
    events: list[dict[str, Any]]
    decisions: list[dict[str, Any]]


EventRule = Callable[[EventRuleContext], EventRuleHit | None]
DecisionRule = Callable[[DecisionRuleContext], list[dict[str, Any]]]


@dataclass
class AppLiftRules:
    event_rules: tuple[EventRule, ...] = ()
    decision_rules: tuple[DecisionRule, ...] = ()


_NO_LIFT_RULES = AppLiftRules()


def _validated_rules(app: str, module: Any, attr: str) -> tuple[Any, ...]:
    rules = getattr(module, attr, ()) or ()
    if not isinstance(rules, (list, tuple)) or not all(callable(r) for r in rules):
        raise LiftError(
            f"apps/{app}/lift_rules.py 的 {attr} 必须是可调用对象的列表（得到 {type(rules).__name__}）")
    return tuple(rules)


def load_app_lift_rules(app: str | None) -> AppLiftRules:
    """加载 `apps/<app>/lift_rules.py` 声明的提升规则；没有该模块 = 没有 app 规则。

    模块不存在是常态（多数 app 用通用路径就够），静默返回空；但模块存在却声明错了
    （EVENT_RULES 不是可调用列表 / 自身 import 报错）一律抛出——那是编码错误，静默降级
    只会让人以为规则在跑。
    """
    name = str(app or "").strip()
    if not name:
        return _NO_LIFT_RULES
    module_path = f"apps.{name}.lift_rules"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        # 缺的是这个模块本身、或它头上的包（app 目录压根没有 python 包）→ 没有专属规则。
        # 缺的是规则模块内部 import 的别的东西 → 那是真错误，别静默当成「没规则」。
        missing = str(exc.name or "")
        if missing and (module_path == missing or module_path.startswith(missing + ".")):
            return _NO_LIFT_RULES
        raise LiftError(f"加载 {module_path} 失败：{exc}") from exc
    return AppLiftRules(
        event_rules=_validated_rules(name, module, "EVENT_RULES"),
        decision_rules=_validated_rules(name, module, "DECISION_RULES"),
    )


def step_event_indices(decisions: list[dict[str, Any]]) -> list[int | None]:
    """decisions → 每个 step 背后的录制事件下标（合成步为 None）。

    提升早就不是「1 event → 1 step」了：语义旁路会把一串手势合并成一个算子，规则也会插入
    没有人手动作的守卫步。回放的「原速」要按录制时的真实停顿来睡，就必须显式带上这张映射，
    否则只能靠数量相等去猜——一旦不等就整块丢弃 timing，人手节奏悄悄没了。

    合并步取**最后**一个事件：`t` 记的是「该动作完成时刻」，一组动作的完成时刻就是最后那下。
    """
    out: list[int | None] = []
    for d in decisions:
        if d.get("synthetic"):
            out.append(None)
            continue
        merged = d.get("source_event_indices")
        if isinstance(merged, list) and merged:
            valid = [int(i) for i in merged if isinstance(i, int) and not isinstance(i, bool) and i >= 0]
            out.append(max(valid) if valid else None)
            continue
        index = d.get("index")
        out.append(int(index) if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None)
    return out


def normalized_step_events(
    step_events: list[int | None] | None, step_count: int,
) -> list[int | None] | None:
    """把一张 step→event 映射规整成可落库的形态；对不上就整块丢弃（宁缺勿错）。

    映射只服务回放节奏，错了会让人手停顿张冠李戴；长度不符 / 全是 None（没有一步对得上
    录制事件）时返回 None，回放退化成均匀节奏——与既有 timing fail-open 同纪律。
    """
    if not isinstance(step_events, list) or len(step_events) != step_count:
        return None
    out: list[int | None] = []
    for value in step_events:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            out.append(None)
        else:
            out.append(value)
    return out if any(v is not None for v in out) else None


def memoized_ocr(ocr_runner: OcrRunner) -> OcrRunner:
    """把一次 lift 内对同一张图的 OCR 收敛成一次。

    一个 tap 会被多条规则连着问同一张 before_shot（版面锚点、行匹配、邻域校验…），
    而 `run_ocr` 是一次 Vision 子进程（实测数百毫秒）。缓存失败结论同样重要：OCR 不可用时
    每条规则各抛一次异常，会把一次提升拖成好几秒的空转。
    """
    cache: dict[str, Any] = {}

    def _run(src: Path) -> dict[str, Any]:
        key = str(src)
        if key not in cache:
            try:
                cache[key] = ocr_runner(src)
            except Exception as exc:  # noqa: BLE001 - 缓存异常本身，见 docstring
                cache[key] = exc
        hit = cache[key]
        if isinstance(hit, Exception):
            raise hit
        return hit

    return _run


def _overlay_marker(src: Path, px: int, py: int) -> tuple[Path | None, Any]:
    """在 before_shot 上画一个红圈+十字标出落点，大幅提升模型坐标接地精度。best-effort：
    缺 Pillow / 图打不开 → 返回 (None, None)，调用方退回「纯文字坐标」提示。返回 (marked_path,
    tmpdir)；tmpdir 须由调用方 cleanup。"""
    try:
        import tempfile

        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - Pillow 缺位不致命，退化成纯坐标提示
        return None, None
    if not Path(src).exists():
        return None, None
    try:
        im = Image.open(src).convert("RGB")
    except Exception:  # noqa: BLE001 - 损坏/非图同样退化
        return None, None
    d = ImageDraw.Draw(im)
    r = 14
    d.ellipse([px - r, py - r, px + r, py + r], outline=(255, 0, 0), width=3)
    d.line([px - r - 8, py, px + r + 8, py], fill=(255, 0, 0), width=2)
    d.line([px, py - r - 8, px, py + r + 8], fill=(255, 0, 0), width=2)
    tmp = tempfile.TemporaryDirectory(prefix="record-lift-mark-")
    out = Path(tmp.name) / "marked.png"
    im.save(out)
    return out, tmp


def _landing_crop(src: Path, px: int, py: int) -> tuple[Path | None, Any]:
    """从 before_shot 裁出落点附近一小块、放大后喂 AI——比整张图看小字/密集控件更清。
    裁框与坐标同 preview 空间（整图 430×932），按 CROP_PW×CROP_PH 居中、夹到图边界内，
    再放大 CROP_UPSCALE 倍并在放大图上画红圈标出同一落点（裁图只喂 AI、不喂 OCR，标记不污染
    识别）。best-effort：缺 Pillow / 图打不开 → (None, None)，调用方退回只喂整图。返回
    (crop_path, tmpdir)；tmpdir 须由调用方 cleanup。"""
    try:
        import tempfile

        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - Pillow 缺位不致命，退回只喂整图
        return None, None
    if not Path(src).exists():
        return None, None
    try:
        im = Image.open(src).convert("RGB")
    except Exception:  # noqa: BLE001 - 损坏/非图同样退化
        return None, None
    w, h = im.size
    cw, ch = min(CROP_PW, w), min(CROP_PH, h)
    left = max(0, min(px - cw // 2, w - cw))
    top = max(0, min(py - ch // 2, h - ch))
    s = CROP_UPSCALE
    crop = im.crop((left, top, left + cw, top + ch)).resize((cw * s, ch * s), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    cx, cy, r = (px - left) * s, (py - top) * s, 12
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=3)
    d.line([cx - r - 6, cy, cx + r + 6, cy], fill=(255, 0, 0), width=2)
    d.line([cx, cy - r - 6, cx, cy + r + 6], fill=(255, 0, 0), width=2)
    tmp = tempfile.TemporaryDirectory(prefix="record-lift-crop-")
    out = Path(tmp.name) / "crop.png"
    crop.save(out)
    return out, tmp


def _positioned_region(px: int, py: int) -> list[float]:
    """「有定位」按钮的回放 region（preview 落点 → 归一化框）。顶栏 / 底栏落点锚成「窄 y 带 + 落点
    横向邻域」（排除正文同名、对左右微漂留容差）；带外落点回落到落点邻域方框（比裸 tap_xy 稳、又不
    过窄）。所有类型都锚 region 是关键：录制期 OCR 唯一命中不代表回放期唯一（正文可能冒出同名文字）。"""
    pw, ph = EXPECTED_PREVIEW
    x0 = round(max(0.0, (px - BAND_REGION_HALF_PX) / pw), 4)
    x1 = round(min(1.0, (px + BAND_REGION_HALF_PX) / pw), 4)
    if py <= ph * TOP_BAND_NY:
        return [x0, 0.0, x1, round(TOP_BAND_NY, 4)]
    if py >= ph * (1.0 - BOTTOM_BAND_NY):
        return [x0, round(1.0 - BOTTOM_BAND_NY, 4), x1, 1.0]
    return [
        round(max(0.0, (px - REGION_HALF_PX) / pw), 4),
        round(max(0.0, (py - REGION_HALF_PX) / ph), 4),
        round(min(1.0, (px + REGION_HALF_PX) / pw), 4),
        round(min(1.0, (py + REGION_HALF_PX) / ph), 4),
    ]


def _ocr_verify_near(
    src: Path,
    px: int,
    py: int,
    label: str,
    *,
    ocr_runner: OcrRunner,
    min_confidence: float,
    element_type: str | None = None,
) -> tuple[str, list[float] | None]:
    """落点附近 OCR 命中校验。返回 (status, region)：
      · ("unavailable", region)：OCR 跑不了（二进制缺 / 图损坏）→ 调用方降级到纯 AI 提升。有定位类型
                                 仍按几何给 region（无需 OCR 即知落点在顶栏/角落），否则 None。
      · ("no_match", None)     ：OCR 里根本没有 label → AI 疑似误读，调用方回退 tap_xy
      · ("far", None)          ：有 label 但既不在落点紧邻、也不在同一行带 → 回退 tap_xy
      · ("verified", region)   ：紧邻命中。有定位类型（POSITIONED_TYPES）**恒锚 region**（几何定位，
                                 排除回放期正文同名飘移）；普通类型维持原有「唯一不收紧 / 多处按落点收紧」。
      · ("verified_row", region)：紧邻没有但同一水平行带内有（场景①整行可点 / 宽按钮，落点落在行内
                                 空白、label 居中或偏左跨大半屏）。region 恒为「全宽 x、窄 y 行带」——回放
                                 只在该行带按文字串选框。**所有类型都保留此兜底**（含有定位类型）：它只在
                                 紧邻近失时才触发，不影响近命中的几何锚 region，却救回宽按钮空白落点。
    坐标全程 preview 像素：Match.nx/ny 是归一化中心，×preview 维度还原成 preview px 比距离与建框。
    """
    positioned = element_type in POSITIONED_TYPES
    pw, ph = EXPECTED_PREVIEW
    try:
        ocr = ocr_runner(src)
    except OcrError:
        return "unavailable", (_positioned_region(px, py) if positioned else None)
    except Exception:  # noqa: BLE001 — 任何 OCR 异常都只降级，不让缺 OCR 拖垮整条 trace
        return "unavailable", (_positioned_region(px, py) if positioned else None)
    matches = find_text(ocr, label, min_confidence=min_confidence)
    if not matches:
        return "no_match", None

    def _dist(m: Match) -> float:
        return math.hypot(px - m.nx * pw, py - m.ny * ph)

    # 有定位类型放宽紧邻半径：右上小字按钮的 tap 点常与 OCR 框中心差 >60px，用默认半径会误判成 far。
    near_radius = BUTTON_NEAR_RADIUS_PX if positioned else NEAR_RADIUS_PX
    if _dist(min(matches, key=_dist)) <= near_radius:
        if positioned:
            # 有定位按钮：录制期唯一也锚 region（回放期未必唯一）——几何锚定排除正文同名飘移。
            return "verified", _positioned_region(px, py)
        # 普通类型：维持原有「唯一不收紧 / 多处按落点收紧方框」消歧。
        if len(matches) < 2:
            return "verified", None
        region = [
            round(max(0.0, (px - REGION_HALF_PX) / pw), 4),
            round(max(0.0, (py - REGION_HALF_PX) / ph), 4),
            round(min(1.0, (px + REGION_HALF_PX) / pw), 4),
            round(min(1.0, (py + REGION_HALF_PX) / ph), 4),
        ]
        return "verified", region

    # 紧邻没命中：退一步看「同一水平行带」——纵向收紧到 ±ROW_BAND_HALF_PY 隔开相邻行，横向不限
    # （整行可点 / 宽按钮，label 与落点可能跨大半屏）。所有类型都走此兜底：有定位类型的近命中已在上面
    # 早返回锚了 region，这里只处理近失，宽按钮空白落点靠同行 label 救回，避免误退成脆弱的 tap_xy。
    if any(abs(m.ny * ph - py) <= ROW_BAND_HALF_PY for m in matches):
        row_region = [
            0.0,
            round(max(0.0, (py - ROW_BAND_HALF_PY) / ph), 4),
            1.0,
            round(min(1.0, (py + ROW_BAND_HALF_PY) / ph), 4),
        ]
        return "verified_row", row_region

    # label 存在但既不紧邻、也不在同一行带 → AI 疑似误读，回退坐标。
    return "far", None


def _native_observed(event: dict[str, Any]) -> bool:
    from autophone.record.native import observed_event_valid
    return observed_event_valid(event)


def _tap_fallback(ev: dict[str, Any]) -> dict[str, Any]:
    # Active input requires a UDS acknowledgement. Native pointer input requires
    # a validated observation and retains unverified delivery in the source trace.
    if (("input_observation" in ev and not _native_observed(ev))
            or (ev.get("uds_result", {}).get("ok") is not True and not _native_observed(ev))):
        raise LiftError(
            f"tap event 缺少有效 UDS 回执或原生观测，preview={ev.get('preview')!r}")
    px, py = int(ev["preview"][0]), int(ev["preview"][1])
    fallback: dict[str, Any] = {"action": "tap_xy", "px": px, "py": py}
    if ev.get("label"):
        fallback["label"] = ev["label"]

    return fallback


def _lift_event(
    ev: dict[str, Any],
    trace_dir: Path,
    *,
    invoker: Invoker,
    ocr_runner: OcrRunner,
    ocr_min_confidence: float,
    accept: set[str],
    app_operations: dict[str, Callable[..., Any]],
    event_rules: tuple[EventRule, ...],
    provider: str | None,
    model: str | None,
    timeout_s: float,
) -> LiftDecision:
    """提升一个 tap 事件。判得准回 tap_text，否则回退 tap_xy（preview 坐标 + 原 label）。"""
    if ev.get("kind") != "tap":  # defense: lift_trace dispatches only tap here
        raise LiftError(f"_lift_event 只处理 tap，误收 kind={ev.get('kind')!r}")
    fallback = _tap_fallback(ev)
    px, py = fallback["px"], fallback["py"]

    if _native_observed(ev):
        return LiftDecision(index=-1, source="passthrough", reason="native_observed_pointer", step=fallback)

    # 提升必须基于截图证据：before_shot 缺失（键缺 / 文件丢）就只回退 tap_xy，绝不让
    # agent 凭纯文字坐标提示「读」一张不存在的图、从而杜撰出 tap_text。注意与「Pillow 缺失」
    # 区分：Pillow 缺只是画不了红圈标记，原始截图仍在，照样喂图提升（_overlay_marker 退化）。
    before_name = ev.get("before_shot")
    src = (trace_dir / before_name) if before_name else None
    if src is None or not src.exists():
        return LiftDecision(index=-1, source="fallback", reason="missing_before_shot",
                            step=fallback)
    # app 专属版面规则先手：它们认的是具体页面（门店行 / 底部主按钮 / 商品卡），比模型读图更准
    # 也更省一次调用。命中记 source="heuristic"——是 OCR 规则不是模型，审查页别把它显示成 AI 高置信。
    rule_ctx = EventRuleContext(
        src=src, px=px, py=py, ocr=ocr_runner,
        min_confidence=ocr_min_confidence, app_operations=app_operations,
    )
    for rule in event_rules:
        hit = rule(rule_ctx)
        if hit is None:
            continue
        return LiftDecision(index=-1, source="heuristic", reason=hit.reason, step=hit.step,
                            confidence="high", ai_text=hit.query,
                            element_type=hit.element_type)
    marked, tmp = _overlay_marker(src, px, py)
    crop, crop_tmp = _landing_crop(src, px, py)
    images = [marked if marked is not None else src]
    if crop is not None:
        images.append(crop)
    marker_line = (
        "（第一张图已用红圈+十字标出该落点。）\n" if marked is not None and crop is not None
        else "（图中已用红圈+十字标出该落点。）\n" if marked is not None
        else ""
    )
    prompt = (
        f"点击落点（preview 坐标，原点左上，整图 {EXPECTED_PREVIEW[0]}×{EXPECTED_PREVIEW[1]}）：({px}, {py})。\n"
        + marker_line
        + ("（第二张图是落点附近的放大裁图，红圈标出同一落点，看小字更清。）\n" if crop is not None else "")
        + (f"本步录制人说明：{str(ev.get('label')).strip()[:MAX_LABEL_LEN]}。\n" if str(ev.get("label") or "").strip() else "")
        + "请识别该落点所在可点击元素的文字；若是纯图标无文字，text 给 null 并尽量在 icon 给出语义。"
    )
    try:
        res = invoker(AiRequest(
            prompt=prompt, system=_SYSTEM, tools="none", json_schema=_LIFT_SCHEMA,
            images=images, provider=provider, model=model, timeout_s=timeout_s))
    finally:
        if tmp is not None:
            tmp.cleanup()
        if crop_tmp is not None:
            crop_tmp.cleanup()

    # 决策（任一不满足即回退，回退恒可回放）：
    if not res.ok:
        return LiftDecision(index=-1, source="fallback", reason=f"ai_error: {res.error}",
                            step=fallback)
    j = res.json or {}
    text = j.get("text")
    conf = j.get("confidence")
    et = j.get("element_type")
    element_type = et if isinstance(et, str) and et else None
    if not (isinstance(text, str) and text.strip()):
        # 纯图标无文字：② 试提升成 tap_ax(a11y label)——region 限落点邻域 + fallback_xy 录制原坐标兜底，
        # AX 命中即稳，猜错 / 无 AX provider 退化成今天的 tap_xy（零回归）。a11y label 创建期无活树可核，
        # 靠这两道护栏夹风险；置信低于 accept 不提升（不拿低置信的图标猜测换坐标兜底）。
        icon = j.get("icon")
        if conf in accept and isinstance(icon, str) and icon in ICON_A11Y_LABELS:
            pw, ph = EXPECTED_PREVIEW
            region = [
                round(max(0.0, (px - REGION_HALF_PX) / pw), 4),
                round(max(0.0, (py - REGION_HALF_PX) / ph), 4),
                round(min(1.0, (px + REGION_HALF_PX) / pw), 4),
                round(min(1.0, (py + REGION_HALF_PX) / ph), 4),
            ]
            a11y = ICON_A11Y_LABELS[icon]
            step = {"action": "tap_ax", "query": a11y, "role": "button",
                    "region": region, "fallback_xy": [px, py]}
            return LiftDecision(index=-1, source="ai", reason=f"lifted_icon({icon})", step=step,
                                confidence=conf, ai_text=a11y, element_type=element_type or "icon")
        return LiftDecision(index=-1, source="fallback", reason="no_text", step=fallback,
                            confidence=conf, element_type=element_type)
    if conf not in accept:
        return LiftDecision(index=-1, source="fallback", reason=f"low_confidence({conf})",
                            step=fallback, confidence=conf, ai_text=text.strip(),
                            element_type=element_type)
    label = text.strip()
    if len(label) > MAX_LABEL_LEN or "\n" in label:
        return LiftDecision(index=-1, source="fallback", reason="text_too_long", step=fallback,
                            confidence=conf, ai_text=label, element_type=element_type)

    # 落点附近 OCR 校验：AI 说了文字也不直接采信，要 OCR 在落点邻域确认同文字才提升。
    # OCR 在原始 src（非红圈标记图）上跑——标记会污染识别。
    status, region = _ocr_verify_near(src, px, py, label, ocr_runner=ocr_runner,
                                      min_confidence=ocr_min_confidence,
                                      element_type=element_type)
    if status in ("no_match", "far"):
        # AI 读出文字但 OCR 在落点附近确认不了 → 这次提升不可信，回退 tap_xy（保留 AI 候选留痕）。
        return LiftDecision(index=-1, source="fallback", reason=f"ocr_{status}", step=fallback,
                            confidence=conf, ai_text=label, element_type=element_type)
    step: dict[str, Any] = {"action": "tap_text", "query": label}
    if region is not None:
        # verified：有定位类型恒锚几何 region / 同文字多处按落点收紧；verified_row：全宽窄 y 行带（场景①）。
        step["region"] = region
    # 留痕 reason 区分提升路径：行带命中 / 紧邻命中 / OCR 不可用降级纯 AI。
    reason = {"verified": "lifted", "verified_row": "lifted_row"}.get(status, "lifted_no_ocr")
    return LiftDecision(index=-1, source="ai", reason=reason, step=step,
                        confidence=conf, ai_text=label, element_type=element_type)


def _passthrough_event(ev: dict[str, Any]) -> LiftDecision:
    """Validate active delivery or native swipe observation before producing draft IR."""
    kind = ev.get("kind")
    if (("input_observation" in ev and not _native_observed(ev))
            or (ev.get("uds_result", {}).get("ok") is not True and not _native_observed(ev))):
        raise LiftError(
            f"{kind} event 缺少有效 UDS 回执或原生观测")
    if kind == "type":
        text = ev.get("text")
        if not (isinstance(text, str) and text):
            raise LiftError(f"type 事件缺合法 text（得到 {text!r}）")
        return LiftDecision(index=-1, source="passthrough", reason="type",
                            step={"action": "type_text", "text": text})
    if kind == "swipe":
        fp, tp = ev.get("from_preview"), ev.get("to_preview")
        if not (isinstance(fp, list) and isinstance(tp, list) and len(fp) == 2 and len(tp) == 2):
            raise LiftError(f"swipe 事件缺合法 from_preview/to_preview（{fp!r} -> {tp!r}）")
        ms = ev.get("ms")
        if not (isinstance(ms, int) and not isinstance(ms, bool) and ms > 0):
            raise LiftError(f"swipe 事件 ms 非正整数（{ms!r}）")
        step: dict[str, Any] = {"action": "swipe_xy", "px1": int(fp[0]), "py1": int(fp[1]),
                                "px2": int(tp[0]), "py2": int(tp[1]), "ms": ms}
        if ev.get("label"):
            step["label"] = ev["label"]
        return LiftDecision(index=-1, source="passthrough", reason="swipe", step=step)
    if kind == "key":
        name = ev.get("name")
        if name not in HARDWARE_KEYS:
            raise LiftError(
                f"key 事件的 name 不在硬件键封闭集内（得到 {name!r}，"
                f"只认 {'/'.join(HARDWARE_KEYS)}）")
        return LiftDecision(index=-1, source="passthrough", reason="key",
                            step={"action": "press_key", "name": str(name)})
    raise LiftError(f"未知事件 kind={kind!r}（只认 tap/type/swipe/key）")


def recorded_steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert each acknowledged recorded event into one deterministic Recipe step."""
    events = trace.get("events")
    if not isinstance(events, list) or not events:
        raise LiftError("the recording contains no events")
    steps = []
    for event in events:
        if not isinstance(event, dict):
            raise LiftError("invalid recording event")
        if event.get("kind") == "tap":
            preview = event.get("preview")
            if not isinstance(preview, list) or len(preview) != 2:
                raise LiftError("tap event requires preview coordinates")
            steps.append(_tap_fallback(event))
        else:
            steps.append(_passthrough_event(event).step)
    return steps


def lift_trace(
    trace: dict[str, Any],
    trace_dir: str | Path,
    *,
    invoker: Invoker = invoke,
    ocr_runner: OcrRunner = run_ocr,
    ocr_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 120.0,
    accept_confidence: tuple[str, ...] = DEFAULT_ACCEPT,
    screen_asset_path: str | Path | None = None,
    screen_statuses: tuple[str, ...] = ("active",),
    screen_conflicts: str = "raise",
    progress: Callable[[dict[str, Any]], None] | None = None,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """trace dict → {"steps": [...typed IR...], "decisions": [...留痕...]}。

    逐 tap 事件提升；最终 step 列表过 validate_recipe（fail-closed）——产不出可回放 recipe 抛
    LiftError。提升是 best-effort，单事件判不准只回退该步，不让整条失败。

    S3 screen context is opt-in via ``screen_asset_path``. When enabled, each
    event's before_shot is classified against screen skeleton assets and emitted
    as top-level ``screen_expectations`` sidecar metadata; recipe step dicts stay
    untouched so validate_recipe remains the execution contract.

    ``screen_conflicts="raise"``（CLI 默认）时多个签名同时命中仍按 S0 fail-loud 抛错；
    ``"record"``（Web 审查路径）时该帧按未识别处理，冲突写进顶层 ``screen_conflicts``，
    屏骨架只是先验，不得让提升失败。
    """
    if screen_conflicts not in {"raise", "record"}:
        raise ValueError(f"screen_conflicts must be raise or record: {screen_conflicts!r}")
    trace_dir = Path(trace_dir)
    accept = set(accept_confidence)
    events = trace.get("events") or []
    app = str(trace.get("app") or "")
    app_operations = _ensure_app_operations(app, root=root)
    app_rules = load_app_lift_rules(app)
    # 一次提升里同一张 before_shot 会被规则和邻域校验反复问；OCR 是子进程，按图缓存。
    ocr_runner = memoized_ocr(ocr_runner)
    if progress is not None:
        progress({
            "type": "progress",
            "phase": "preparing",
            "progress": 5,
            "message": f"准备分析 {len(events)} 个录制动作",
            "completed": 0,
            "total": len(events),
        })
    # 空 trace → 空 steps，validate_recipe([]) 不报错但 load_recipe/operation schema 要非空。
    # 拒绝产一条不可回放的空 recipe（同 M1 铁律：产不出可回放 recipe 就抛）。
    if not events:
        raise LiftError("trace 无事件，提升不出可回放 recipe（拒绝产空 recipe）")
    steps: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    screen_doc = screen_assets.load_screen_asset(Path(screen_asset_path)) if screen_asset_path else None
    before_screens: dict[int, str] = {}
    screen_matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if progress is not None:
            progress({
                "type": "progress",
                "phase": "analyzing_actions",
                "progress": 10 + round(65 * i / len(events)),
                "message": f"正在分析动作 {i + 1}/{len(events)}",
                "completed": i,
                "total": len(events),
            })
        try:
            screen_match = _classify_before_shot(
                ev,
                trace_dir,
                screen_doc=screen_doc,
                ocr_runner=ocr_runner,
                statuses=screen_statuses,
            )
        except screen_assets.ScreenSignatureConflict as exc:
            if screen_conflicts == "raise":
                raise
            screen_match = None
            conflicts.append({"index": i, "error": str(exc)[:240]})
        if screen_match is not None:
            before_screens[i] = screen_match.screen_id
            screen_matches.append({"index": i, **screen_match.as_dict()})
        # tap 走 AI 提升（tap_text 或回退 tap_xy）；type/swipe/key 无语义可猜，原样直通。
        if ev.get("kind") == "tap":
            d = _lift_event(ev, trace_dir, invoker=invoker, ocr_runner=ocr_runner,
                            ocr_min_confidence=ocr_min_confidence, accept=accept,
                            app_operations=app_operations,
                            event_rules=app_rules.event_rules,
                            provider=provider, model=model, timeout_s=timeout_s)
        else:
            d = _passthrough_event(ev)
        if screen_match is not None:
            _apply_screen_region_hint(d, ev, screen_doc, screen_match)
        d.index = i
        # 回填审查 UI 所需的接地信息（落点 + before_shot），tap 才有落点、passthrough 仅带图。
        if ev.get("kind") == "tap" and isinstance(ev.get("preview"), list) and len(ev["preview"]) == 2:
            d.preview = [int(ev["preview"][0]), int(ev["preview"][1])]
        bshot = ev.get("before_shot")
        d.before_shot = bshot if isinstance(bshot, str) and bshot else None
        decision = asdict(d)
        if screen_match is not None:
            decision["screen_id"] = screen_match.screen_id
        decisions.append(decision)
        if progress is not None:
            progress({
                "type": "progress",
                "phase": "analyzing_actions",
                "progress": 10 + round(65 * (i + 1) / len(events)),
                "message": f"已分析 {i + 1}/{len(events)} 个动作",
                "completed": i + 1,
                "total": len(events),
            })

    for decision_rule in app_rules.decision_rules:
        decisions = decision_rule(DecisionRuleContext(
            app=app, trace=trace, events=events, decisions=decisions,
        ))
    steps = [d["step"] for d in decisions]

    # fail-closed 闸：提升出来的 IR 必须可回放（瞎编 verb / 名对值错一律拦）。理论上我们只产
    # tap_text(query) / tap_xy(px,py,label?) 两种合法形态，这里是兜底纪律、与 ai_draft 同款。
    try:
        if progress is not None:
            progress({
                "type": "progress",
                "phase": "validating_steps",
                "progress": 78,
                "message": "正在校验可回放步骤",
            })
        if app and app_operations:
            with operation_scope(app, root=root):
                validate_recipe(steps)
        else:
            validate_recipe(steps)
    except RecipeError as e:
        raise LiftError(f"提升出的 IR 过不了 validate_recipe（拒绝产不可回放 recipe）：{e}") from e

    n_lifted = sum(1 for d in decisions if d["source"] in {"ai", "semantic", "heuristic"})
    n_pass = sum(1 for d in decisions if d["source"] == "passthrough")
    out = {
        "lift_version": LIFT_PREVIEW_VERSION,
        "steps": steps,
        "decisions": decisions,
        # 每个 step 由哪个录制事件而来（合并步取最后一个，合成步为 null）——回放要按它把
        # 人手真实停顿对回 steps，不能再假设「1 event → 1 step」（§倍速回放）。
        "step_events": step_event_indices(decisions),
        "summary": {"events": len(events), "lifted": n_lifted,
                    "fallback": len(decisions) - n_lifted - n_pass, "passthrough": n_pass},
    }
    has_script = bool(_script_steps(
        trace.get("script") if isinstance(trace.get("script"), dict) else None
    ))
    if progress is not None:
        progress({
            "type": "progress",
            "phase": "aligning_description" if has_script else "finishing",
            "progress": 84 if has_script else 96,
            "message": "正在生成说明与动作关联" if has_script else "正在整理分析结果",
        })
    alignment, alignment_error = _align_script(
        trace.get("script") if isinstance(trace.get("script"), dict) else None,
        decisions,
        invoker=invoker,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
    )
    if alignment is not None:
        out["script_alignment"] = alignment
    if alignment_error:
        out["alignment_error"] = alignment_error
    if progress is not None:
        progress({
            "type": "progress",
            "phase": "finishing",
            "progress": 98,
            "message": "正在整理可编辑结果",
        })
    if screen_doc is not None:
        screen_expectations = _derive_screen_expectations(before_screens)
        if 0 in before_screens:
            out["screen_starts_with"] = before_screens[0]
        out["screen_expectations"] = screen_expectations
        out["screen_matches"] = screen_matches
        if conflicts:
            out["screen_conflicts"] = conflicts
    return out


def classify_trace_screens(
    trace: dict[str, Any],
    trace_dir: str | Path,
    *,
    screen_asset_path: str | Path,
    screen_statuses: tuple[str, ...] = ("active",),
    ocr_runner: OcrRunner = run_ocr,
) -> dict[str, Any]:
    """只做页分类，不提升：每个事件的 before_shot → 命中的 screen（结构化 OCR，不调 AI）。

    Web 审查落库时 steps 已由人审定，不再跑 lift；页级 checkpoint 的输入（每个事件执行前
    在哪一页）由宿主按当前资产重新分类得到，不信任浏览器回传。签名冲突按未识别处理并留痕。
    返回 ``{"matches": [{index, screen_id, status, ...}], "conflicts": [{index, error}]}``。
    """
    trace_dir = Path(trace_dir)
    screen_doc = screen_assets.load_screen_asset(Path(screen_asset_path))
    runner = memoized_ocr(ocr_runner)
    matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for i, ev in enumerate(trace.get("events") or []):
        if not isinstance(ev, dict):
            continue
        try:
            match = _classify_before_shot(
                ev, trace_dir, screen_doc=screen_doc, ocr_runner=runner, statuses=screen_statuses,
            )
        except screen_assets.ScreenSignatureConflict as exc:
            conflicts.append({"index": i, "error": str(exc)[:240]})
            continue
        if match is not None:
            matches.append({"index": i, **match.as_dict()})
    return {"matches": matches, "conflicts": conflicts}


def lift_trace_file(trace_ref: str | Path, **kwargs: Any) -> dict[str, Any]:
    """从 trace.json 路径（或其所在目录）提升。trace_dir 取该文件所在目录（before_shot 在那）。"""
    p = Path(trace_ref)
    trace_path = p / "trace.json" if p.is_dir() else p
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    return lift_trace(trace, trace_path.parent, **kwargs)


def lift_trace_event(
    trace: dict[str, Any],
    trace_dir: str | Path,
    index: int,
    *,
    invoker: Invoker = invoke,
    ocr_runner: OcrRunner = run_ocr,
    ocr_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 120.0,
    accept_confidence: tuple[str, ...] = DEFAULT_ACCEPT,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """Retry-lift a single trace event for the review UI.

    This intentionally returns one decision only and does not validate the full
    recipe; the final /land path still validates the edited step list as the
    durable execution contract.
    """
    trace_dir = Path(trace_dir)
    events = trace.get("events") or []
    if not isinstance(index, int) or index < 0 or index >= len(events):
        raise LiftError(f"event index out of range: {index}")
    ev = events[index]
    if not isinstance(ev, dict):
        raise LiftError(f"event index {index} is not an object")
    app = str(trace.get("app") or "")
    app_operations = _ensure_app_operations(app, root=root)
    accept = set(accept_confidence)
    if ev.get("kind") == "tap":
        d = _lift_event(
            ev,
            trace_dir,
            invoker=invoker,
            ocr_runner=memoized_ocr(ocr_runner),
            ocr_min_confidence=ocr_min_confidence,
            accept=accept,
            app_operations=app_operations,
            # 单步重试与整条提升必须同一套规则，否则审查页上「重试这一步」会给出与
            # 其它步不同口径的结果。
            event_rules=load_app_lift_rules(app).event_rules,
            provider=provider,
            model=model,
            timeout_s=timeout_s,
        )
    else:
        d = _passthrough_event(ev)
    d.index = index
    if ev.get("kind") == "tap" and isinstance(ev.get("preview"), list) and len(ev["preview"]) == 2:
        d.preview = [int(ev["preview"][0]), int(ev["preview"][1])]
    bshot = ev.get("before_shot")
    d.before_shot = bshot if isinstance(bshot, str) and bshot else None
    decision = asdict(d)
    return {"decision": decision, "step": decision["step"]}


def _classify_before_shot(
    ev: dict[str, Any],
    trace_dir: Path,
    *,
    screen_doc: dict[str, Any] | None,
    ocr_runner: OcrRunner,
    statuses: tuple[str, ...],
) -> screen_assets.ScreenMatch | None:
    if screen_doc is None:
        return None
    bshot = ev.get("before_shot")
    if not (isinstance(bshot, str) and bshot):
        return None
    src = trace_dir / bshot
    if not src.exists():
        return None
    try:
        ocr = ocr_runner(src)
    except OcrError:
        return None
    return screen_assets.match_screens(ocr, screen_doc.get("screens") or [], statuses=statuses)


def _derive_screen_expectations(before_screens: dict[int, str]) -> dict[int, str]:
    expectations: dict[int, str] = {}
    for i in sorted(before_screens):
        if i <= 0 or i - 1 not in before_screens:
            continue
        current = before_screens[i - 1]
        nxt = before_screens[i]
        if current != nxt:
            expectations[i - 1] = nxt
    return expectations


def _apply_screen_region_hint(
    decision: LiftDecision,
    ev: dict[str, Any],
    screen_doc: dict[str, Any] | None,
    screen_match: screen_assets.ScreenMatch,
) -> None:
    if screen_doc is None or decision.step.get("action") != "tap_text" or "region" in decision.step:
        return
    preview = ev.get("preview")
    if not (isinstance(preview, list) and len(preview) == 2):
        return
    try:
        screen = screen_assets.screen_by_id(screen_doc, screen_match.screen_id)
        region = screen_assets.region_for_point(screen, preview)
    except (KeyError, screen_assets.ScreenAssetError):
        return
    if region is None:
        return
    _name, box = region
    decision.step["region"] = box


def alignment_note(alignment: dict[str, Any]) -> str:
    groups = alignment.get("groups") if isinstance(alignment, dict) else []
    if not isinstance(groups, list):
        return "aligned=invalid"
    matched = sum(1 for g in groups if isinstance(g, dict) and g.get("verdict") == "matched")
    missing = sum(1 for g in groups if isinstance(g, dict) and g.get("verdict") == "missing")
    extra = sum(1 for g in groups if isinstance(g, dict) and g.get("verdict") == "extra")
    src = str(alignment.get("script_source") or "script")
    return f"aligned={src} matched {matched} missing {missing} extra {extra}"


def land_lifted_operation(
    app: str,
    name: str,
    summary: str,
    steps: list[dict[str, Any]],
    *,
    screen_starts_with: str | None = None,
    screen_expectations: dict[int, str] | dict[str, str] | None = None,
    script_alignment: dict[str, Any] | None = None,
    from_ref: str | None = None,
    step_events: list[int | None] | None = None,
    provider: str | None = None,
    model: str | None = None,
    ops_path: str | Path | None = None,
    required_passes: int = 3,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """把提升出的 typed IR steps 落库为一条 operation draft（M2「落库」）。

    复用 AI-4 的 `land_operation_draft`——同一套 fail-closed 落库闸（内容 schema / IR dispatcher /
    重名 / 整库 validate_or_raise）+ 转正门读数，录制与 AI 起草不各写一份。`name`/`summary` 由人在
    落库时给（trace 里没有），steps 是 lifter 产物。provenance 恒 `source: conversation`（§3.4 铁律①：
    录制轨迹冻结也算对话来源，绝不写 source: ai）；录制+提升的来源打进 note，trace 目录进 `from`。
    产物恒 `status=draft`——录制不自行入回归库，转正仍走人审 + 连续 passed 闸（§五）。"""
    assert_assets_writable("land lifted operation", path=ops_path)
    from autophone.application.ai_draft import land_operation_draft

    note = "recorded via record_capture + lifted via record_lift"
    if script_alignment:
        note += f"; {alignment_note(script_alignment)}"
    if provider or model:
        note += f"（lift agent {provider or 'default'}{'/' + model if model else ''}）"
    provenance: dict[str, Any] = {"source": "conversation", "note": note}
    if from_ref:
        provenance["from"] = from_ref
    mapping = normalized_step_events(step_events, len(steps))
    if mapping is not None:
        provenance["step_events"] = mapping
    fields: dict[str, Any] = {"name": name, "summary": summary, "steps": steps}
    if screen_starts_with:
        fields["screen_starts_with"] = screen_starts_with
    if screen_expectations:
        fields["screen_expectations"] = {str(k): str(v) for k, v in screen_expectations.items()}
    return land_operation_draft(
        app, fields, provenance=provenance,
        ops_path=Path(ops_path) if ops_path is not None else None,
        required_passes=required_passes,
        root=root,
    )


def update_lifted_operation(
    app: str,
    op_id: str,
    name: str,
    summary: str,
    steps: list[dict[str, Any]],
    *,
    screen_starts_with: str | None = None,
    screen_expectations: dict[int, str] | dict[str, str] | None = None,
    script_alignment: dict[str, Any] | None = None,
    from_ref: str | None = None,
    step_events: list[int | None] | None = None,
    provider: str | None = None,
    model: str | None = None,
    ops_path: str | Path | None = None,
    required_passes: int = 3,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """更新已存在的 operation draft（录制重新提升的落库路径）。

    与 land_lifted_operation 并列：同一套 provenance/fields 组装，但调 update_operation_draft
    而非 land_operation_draft，以就地更新已关联的 draft（revision++）。拒绝更新 active/archived。"""
    assert_assets_writable("update lifted operation", path=ops_path)
    from autophone.application.ai_draft import update_operation_draft

    note = "re-lifted via record_lift"
    if script_alignment:
        note += f"; {alignment_note(script_alignment)}"
    if provider or model:
        note += f"（lift agent {provider or 'default'}{'/' + model if model else ''}）"
    provenance: dict[str, Any] = {"source": "conversation", "note": note}
    if from_ref:
        provenance["from"] = from_ref
    mapping = normalized_step_events(step_events, len(steps))
    if mapping is not None:
        provenance["step_events"] = mapping
    fields: dict[str, Any] = {"name": name, "summary": summary, "steps": steps}
    if screen_starts_with:
        fields["screen_starts_with"] = screen_starts_with
    if screen_expectations:
        fields["screen_expectations"] = {str(k): str(v) for k, v in screen_expectations.items()}
    return update_operation_draft(
        app, op_id, fields, provenance=provenance,
        ops_path=Path(ops_path) if ops_path is not None else None,
        required_passes=required_passes,
        root=root,
    )


# ── selftest：无 VM / 无模型，证「提升决策 + 回退 + validate_recipe 闸」契约 ──────────
def _fake_invoker(results: list[AiResult]) -> Invoker:
    """按事件顺序吐预设 AiResult 的假 invoker（不读图、不调模型）。"""
    it = iter(results)

    def _inv(_req: AiRequest) -> AiResult:
        return next(it)

    return _inv


def _ocr_box(text: str, cx: float, cy: float, *, w: float = 60, h: float = 24,
             conf: float = 0.95) -> dict[str, Any]:
    """造一个中心在 (cx,cy)（preview 像素）的 OCR box。"""
    return {"text": text, "x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h, "confidence": conf}


def _fake_ocr(boxes: list[dict[str, Any]]) -> OcrRunner:
    """返回固定 boxes 的假 ocr_runner（preview 尺寸 430×932，不读真图、不跑二进制）。"""
    def _run(_src: Path) -> dict[str, Any]:
        return {"image_w": EXPECTED_PREVIEW[0], "image_h": EXPECTED_PREVIEW[1], "boxes": list(boxes)}

    return _run


def _ocr_unavailable(_src: Path) -> dict[str, Any]:
    """模拟 OCR 二进制缺位 / 图损坏：抛 OcrError，验证降级到纯 AI 提升。"""
    raise OcrError("ocr binary missing (selftest)")


def _run_selftest() -> int:
    import tempfile

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")

    trace = {
        "schema": "record-trace.v0",
        "app": "food_ordering", "build": "002",
        "start": {"screen_fp": "deadbeef", "starts_with": "reset_to_home"},
        "geometry": {"preview": [430, 932], "native": [1290, 2796], "native_scale": 3},
        "events": [
            {"kind": "tap", "preview": [215, 617], "native": [645, 1851],
             "before_shot": "shot_001.jpg", "after_shot": "shot_002.jpg",
             "uds_result": {"ok": True}, "t": 0.0, "label": ""},
            {"kind": "tap", "preview": [40, 880], "native": [120, 2640],
             "before_shot": "shot_003.jpg", "after_shot": "shot_004.jpg",
             "uds_result": {"ok": True}, "t": 1.0, "label": "我的"},
            {"kind": "tap", "preview": [100, 100], "native": [300, 300],
             "before_shot": "shot_005.jpg", "after_shot": "shot_006.jpg",
             "uds_result": {"ok": True}, "t": 2.0, "label": ""},
            {"kind": "tap", "preview": [300, 500], "native": [900, 1500],
             "before_shot": "shot_007.jpg", "after_shot": "shot_008.jpg",
             "uds_result": {"ok": True}, "t": 3.0, "label": "提交订单"},
        ],
    }
    # 事件→预设：① high 文字命中；② low 置信回退；③ AI 出错回退；④ text 为 null 回退。
    results = [
        AiResult(ok=True, text="", provider="fake", json={"text": "确认", "confidence": "high"}),
        AiResult(ok=True, text="", provider="fake", json={"text": "返回", "confidence": "low"}),
        AiResult(ok=False, text="", provider="fake", error="boom"),
        AiResult(ok=True, text="", provider="fake", json={"text": None, "confidence": "high"}),
    ]
    # 决策逻辑须在「有截图证据」前提下验证——给每个 before_shot 落一个真实文件，否则现在会
    # 被 missing_before_shot 提前回退（正是本次修的缺口）。stub 非真 JPEG：Pillow 打不开 →
    # _overlay_marker 退化成无标记，但原图仍喂给（假）invoker，决策路径照常走。
    td = tempfile.TemporaryDirectory(prefix="record-lift-selftest-")
    tdir = Path(td.name)
    for ev in trace["events"]:
        (tdir / ev["before_shot"]).write_bytes(b"\xff\xd8\xff\xe0stub")
    # event0（落点 215,617，AI=「确认」high）→ OCR 在落点处确认到唯一「确认」框 → verified、不收紧 region。
    out = lift_trace(trace, tdir, invoker=_fake_invoker(results),
                     ocr_runner=_fake_ocr([_ocr_box("确认", 215, 617)]))
    td.cleanup()
    steps = out["steps"]

    check(steps[0] == {"action": "tap_text", "query": "确认"},
          "high 置信 + OCR 落点命中 → tap_text(query) 提升（唯一命中不收紧 region）")
    check(steps[1] == {"action": "tap_xy", "px": 40, "py": 880, "label": "我的"},
          "low 置信 → 回退 tap_xy（preview 坐标 + 原 label 保留）")
    check(steps[2] == {"action": "tap_xy", "px": 100, "py": 100},
          "AI 出错 → 回退 tap_xy（无 label 不强加）")
    check(steps[3] == {"action": "tap_xy", "px": 300, "py": 500, "label": "提交订单"},
          "text=null → 回退 tap_xy")
    check(all("native" not in s and "x" not in s and "y" not in s for s in steps),
          "没有 native / x / y 坐标泄漏进任何 step")
    check(out["summary"] == {"events": 4, "lifted": 1, "fallback": 3, "passthrough": 0},
          "summary 计数正确（1 提升 / 3 回退 / 0 直通）")
    check([d["reason"] for d in out["decisions"]][:4]
          == ["lifted", "low_confidence(low)", "ai_error: boom", "no_text"],
          "decisions 留痕每步回退原因")
    check(all(d["preview"] == ev["preview"] and d["before_shot"] == ev["before_shot"]
              for d, ev in zip(out["decisions"], trace["events"])),
          "每个 tap decision 回填落点 preview + before_shot（审查 UI 画红圈用）")

    # 闸：提升出的 IR 必须真能过引擎静态校验
    try:
        validate_recipe(steps)
        check(True, "提升出的 steps 过 validate_recipe（可回放形态）")
    except RecipeError as e:
        check(False, f"提升出的 steps 被 validate_recipe 拒绝：{e}")

    # ── 落点附近 OCR 校验：消歧 region / no_match / far / unavailable 四条路径 ────────────
    def _one_tap_lift(px: int, py: int, ai_text: str, ocr_runner: OcrRunner,
                      conf: str = "high",
                      element_type: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="record-lift-ocr-") as d:
            (Path(d) / "s.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
            tr = {"events": [{"kind": "tap", "preview": [px, py], "native": [px * 3, py * 3],
                              "before_shot": "s.jpg", "uds_result": {"ok": True}, "t": 0.0,
                              "label": "兜底"}]}
            aj: dict[str, Any] = {"text": ai_text, "confidence": conf}
            if element_type is not None:
                aj["element_type"] = element_type
            o = lift_trace(
                tr, d, ocr_runner=ocr_runner,
                invoker=_fake_invoker([AiResult(ok=True, text="", provider="fake", json=aj)]))
            return o["steps"][0], o["decisions"][0]

    # A) 同文字出现两处（近 + 远）→ 提升并按落点收紧 region（消歧）
    step_a, dec_a = _one_tap_lift(366, 863, "支付",
                                  _fake_ocr([_ocr_box("支付", 366, 863), _ocr_box("支付", 50, 200)]))
    check(step_a == {"action": "tap_text", "query": "支付",
                     "region": [0.6419, 0.8294, 1.0, 1.0]},
          "同文字多处 → tap_text + 按落点收紧 region 消歧")
    check(dec_a["source"] == "ai" and dec_a["reason"] == "lifted", "歧义命中仍记 source=ai/lifted")

    # B) OCR 整页没有该文字 → AI 疑似误读，回退 tap_xy
    step_b, dec_b = _one_tap_lift(366, 863, "支付", _fake_ocr([_ocr_box("首页", 40, 880)]))
    check(step_b == {"action": "tap_xy", "px": 366, "py": 863, "label": "兜底"},
          "OCR 无该文字 → 回退 tap_xy（保留 ev label）")
    check(dec_b["source"] == "fallback" and dec_b["reason"] == "ocr_no_match"
          and dec_b["ai_text"] == "支付", "回退留痕 reason=ocr_no_match + AI 候选")

    # C) OCR 有该文字但只在远处（落点邻域无）→ 回退 tap_xy
    step_c, dec_c = _one_tap_lift(366, 863, "支付", _fake_ocr([_ocr_box("支付", 50, 100)]))
    check(step_c == {"action": "tap_xy", "px": 366, "py": 863, "label": "兜底"},
          "OCR 仅远处有该文字 → 回退 tap_xy")
    check(dec_c["reason"] == "ocr_far", "回退留痕 reason=ocr_far")

    # D) OCR 跑不了（二进制缺 / 图损坏）→ 降级到纯 AI 提升，不回退坐标
    step_d, dec_d = _one_tap_lift(366, 863, "支付", _ocr_unavailable)
    check(step_d == {"action": "tap_text", "query": "支付"},
          "OCR 不可用 → 降级纯 AI 提升 tap_text（不收紧 region）")
    check(dec_d["source"] == "ai" and dec_d["reason"] == "lifted_no_ocr",
          "降级留痕 reason=lifted_no_ocr（区分有没有过 OCR 校验）")

    # ── element_type 驱动的 region 策略（A+D）：有定位类型恒锚 region、放宽紧邻半径 ──────────
    # P1) 右上角「预约」小按钮：唯一命中，普通类型会 verified 不收紧 region（回放子串匹配会飘到正文
    #     「可预约」）；标 element_type=button 且落点在顶栏带 → 锚成「窄 y 顶栏带 + 横向邻域」region。
    step_p1, dec_p1 = _one_tap_lift(392, 96, "预约", _fake_ocr([_ocr_box("预约", 392, 96)]),
                                    element_type="button")
    check(step_p1 == {"action": "tap_text", "query": "预约",
                      "region": [0.6558, 0.0, 1.0, 0.14]},
          "P1 顶栏 button 唯一命中 → tap_text + 顶栏窄带 region（右上锚定，排除正文同名）")
    check(dec_p1["reason"] == "lifted" and dec_p1["element_type"] == "button",
          "P1 留痕 element_type=button")
    # P2) 底栏 tab（如「我的」）：落点在底栏带 → 锚成底栏窄带 region。
    step_p2, _ = _one_tap_lift(215, 900, "我的", _fake_ocr([_ocr_box("我的", 215, 900)]),
                               element_type="tab")
    check(step_p2 == {"action": "tap_text", "query": "我的",
                      "region": [0.2442, 0.9, 0.7558, 1.0]},
          "P2 底栏 tab 唯一命中 → tap_text + 底栏窄带 region")
    # P3) 带外的有定位按钮（页面中部 dialog 按钮）：唯一命中也锚落点邻域方框（普通类型此处会 None）。
    step_p3, _ = _one_tap_lift(215, 500, "确认", _fake_ocr([_ocr_box("确认", 215, 500)]),
                               element_type="dialog")
    check(step_p3 == {"action": "tap_text", "query": "确认",
                      "region": [0.2907, 0.4399, 0.7093, 0.633]},
          "P3 带外 dialog 按钮唯一命中 → tap_text + 落点邻域方框（普通类型会不收紧）")
    # P4) 放宽半径救回右上小字：OCR 框中心距落点 80px（>60 默认、<100 button）——button 判 verified，
    #     普通类型会判 far→退坐标。证明 element_type 把否则会丢的提升救了回来。
    step_p4, dec_p4 = _one_tap_lift(392, 96, "预约", _fake_ocr([_ocr_box("预约", 392, 176)]),
                                    element_type="button")
    check(dec_p4["reason"] == "lifted" and step_p4["action"] == "tap_text",
          "P4 button 放宽半径（80px）仍 verified（普通类型会 far 退坐标）")
    step_p4b, dec_p4b = _one_tap_lift(392, 96, "预约", _fake_ocr([_ocr_box("预约", 392, 176)]))
    check(dec_p4b["reason"] == "ocr_far" and step_p4b["action"] == "tap_xy",
          "P4b 同距离无 element_type → far 退坐标（对照证明放宽半径生效）")
    # P5) list_row 非有定位类型 → 保持原行带/唯一不收紧行为（不被新逻辑波及）。
    step_p5, dec_p5 = _one_tap_lift(215, 500, "支付", _fake_ocr([_ocr_box("支付", 215, 500)]),
                                    element_type="list_row")
    check(step_p5 == {"action": "tap_text", "query": "支付"} and dec_p5["reason"] == "lifted",
          "P5 list_row 唯一命中 → 维持不收紧 region（非有定位类型行为不变）")
    # P6) 有定位类型 + OCR 不可用 → 仍按几何给 region（无需 OCR 即知落点在顶栏）。
    step_p6, dec_p6 = _one_tap_lift(392, 96, "预约", _ocr_unavailable, element_type="button")
    check(step_p6 == {"action": "tap_text", "query": "预约", "region": [0.6558, 0.0, 1.0, 0.14]}
          and dec_p6["reason"] == "lifted_no_ocr",
          "P6 有定位类型 + OCR 不可用 → 几何 region 兜底（顶栏带）")
    validate_recipe([step_p1, step_p2, step_p3, step_p5, step_p6])  # 新 region 形态过引擎静态闸

    # ── ① 行级放宽：整行可点、落点落在行内空白，label 同 y 但横向跨大半屏 ──────────────
    # 落点 (380,600) 在行尾空白，行 label「不需要餐具」OCR 在 (90,600)——同 y、dx=290（远超
    # NEAR_RADIUS）。紧邻不命中但同一行带命中 → tap_text + 全宽窄 y 行带 region（回放在该行带选框）。
    step_e, dec_e = _one_tap_lift(380, 600, "不需要餐具", _fake_ocr([_ocr_box("不需要餐具", 90, 600)]))
    check(step_e == {"action": "tap_text", "query": "不需要餐具",
                     "region": [0.0, 0.6137, 1.0, 0.6738]},
          "①行内空白落点 → tap_text + 全宽窄 y 行带 region（横向不限、纵向收紧隔开相邻行）")
    check(dec_e["source"] == "ai" and dec_e["reason"] == "lifted_row",
          "行带命中留痕 reason=lifted_row")
    # 同文字在另一行（y 差出行带）不污染：落点行带只圈中被点行；远处同名只靠紧邻/行带都够不着 → far。
    step_e2, dec_e2 = _one_tap_lift(
        380, 600, "餐具", _fake_ocr([_ocr_box("餐具", 90, 600), _ocr_box("餐具", 90, 300)]))
    check(dec_e2["reason"] == "lifted_row" and step_e2["region"] == [0.0, 0.6137, 1.0, 0.6738],
          "同文字跨多行 → 行带只圈被点行（300 那个 y 差出带外不干扰）")

    # ── ② 纯图标 → tap_ax（region 限邻域 + fallback_xy 录制坐标兜底，猜错/无 AX 退化 tap_xy）──
    def _icon_lift(px: int, py: int, icon: Any, conf: str = "high",
                   text: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="record-lift-icon-") as d:
            (Path(d) / "s.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
            tr = {"events": [{"kind": "tap", "preview": [px, py], "native": [px * 3, py * 3],
                              "before_shot": "s.jpg", "uds_result": {"ok": True}, "t": 0.0,
                              "label": "兜底"}]}
            o = lift_trace(
                tr, d, ocr_runner=_ocr_unavailable,  # 图标路径不读 OCR，给个会抛的也无妨
                invoker=_fake_invoker([AiResult(ok=True, text="", provider="fake",
                                                json={"text": text, "confidence": conf,
                                                      "icon": icon})]))
            return o["steps"][0], o["decisions"][0]

    step_f, dec_f = _icon_lift(20, 60, "close")  # 左上角关闭 X
    check(step_f == {"action": "tap_ax", "query": "关闭", "role": "button",
                     "region": [0.0, 0.0, 0.2558, 0.1609], "fallback_xy": [20, 60]},
          "②纯图标 close → tap_ax(关闭) + region 限邻域 + fallback_xy 录制坐标兜底")
    check(dec_f["source"] == "ai" and dec_f["reason"] == "lifted_icon(close)"
          and dec_f["ai_text"] == "关闭", "图标提升留痕 reason=lifted_icon(close)")
    # 低置信图标不提升（不拿低置信猜测换坐标兜底）→ 回退 tap_xy
    step_g, dec_g = _icon_lift(20, 60, "close", conf="low")
    check(step_g == {"action": "tap_xy", "px": 20, "py": 60, "label": "兜底"}
          and dec_g["reason"] == "no_text", "低置信图标 → 回退 tap_xy（reason=no_text）")
    # 词表外图标语义 → 回退 tap_xy（schema enum 兜底外再防一手）
    step_h, _ = _icon_lift(20, 60, "frobnicate")
    check(step_h == {"action": "tap_xy", "px": 20, "py": 60, "label": "兜底"},
          "词表外 icon → 回退 tap_xy（不瞎提 tap_ax）")
    # icon 为 null（纯图标但 AI 也判不出语义）→ 回退 tap_xy
    step_i, _ = _icon_lift(20, 60, None)
    check(step_i == {"action": "tap_xy", "px": 20, "py": 60, "label": "兜底"},
          "icon=null → 回退 tap_xy")
    # 提升出的 tap_ax 必须过引擎静态闸（region/fallback_xy/role/query 都在白名单）
    validate_recipe([step_f])

    # ── M3：type/swipe 原样直通（无语义可猜，不喂模型）──────────────────────────
    m3 = {"events": [
        {"kind": "tap", "preview": [215, 617], "native": [645, 1851],
         "before_shot": "gone.jpg", "uds_result": {"ok": True}, "t": 0.0, "label": "兜底"},
        {"kind": "type", "text": "13800138000", "before_shot": "b1.jpg",
         "after_shot": "a1.jpg", "uds_result": {"ok": True}, "t": 1.0},
        {"kind": "swipe", "from_preview": [215, 800], "to_preview": [215, 300],
         "from_native": [645, 2400], "to_native": [645, 900], "ms": 400,
         "before_shot": "b2.jpg", "after_shot": "a2.jpg", "uds_result": {"ok": True},
         "t": 2.0, "label": "上滑"},
        {"kind": "key", "name": "home", "before_shot": "b3.jpg", "after_shot": "a3.jpg",
         "uds_result": {"ok": True}, "t": 3.0},
    ]}
    # tap 的 before_shot 缺失 → 回退 tap_xy（不调 invoker）；type/swipe 直通（也不调 invoker）。
    m3out = lift_trace(m3, "/nonexistent", invoker=_fake_invoker([]))
    check(m3out["steps"][0] == {"action": "tap_xy", "px": 215, "py": 617, "label": "兜底"},
          "M3 混合 trace：tap 仍走提升/回退")
    check(m3out["steps"][1] == {"action": "type_text", "text": "13800138000"},
          "type 事件直通 type_text(text)")
    check(m3out["steps"][2] == {"action": "swipe_xy", "px1": 215, "py1": 800,
                                "px2": 215, "py2": 300, "ms": 400, "label": "上滑"},
          "swipe 事件直通 swipe_xy（preview 坐标 + ms + label，无 native 泄漏）")
    check(m3out["steps"][3] == {"action": "press_key", "name": "home"},
          "key 事件直通 press_key(name)")
    check(all("native" not in s and "x1" not in s for s in m3out["steps"]),
          "M3 直通 step 无 native/native 别名泄漏")
    check([d["source"] for d in m3out["decisions"]] == ["fallback"] + ["passthrough"] * 3
          and [d["reason"] for d in m3out["decisions"]] == ["missing_before_shot", "type", "swipe", "key"],
          "decisions 留痕 tap 回退 + type/swipe/key 直通")
    check(m3out["summary"] == {"events": 4, "lifted": 0, "fallback": 1, "passthrough": 3},
          "M3 summary 计数（0 提升 / 1 回退 / 3 直通）")
    validate_recipe(m3out["steps"])  # 直通产物也必须过引擎静态闸

    # 幽灵 type / swipe（uds_result.ok≠True）fail-closed——没落地的操作不进 recipe
    for ghost_ev, tag in (
        ({"kind": "type", "text": "x", "uds_result": {"ok": False}}, "type"),
        ({"kind": "swipe", "from_preview": [1, 2], "to_preview": [3, 4], "ms": 300,
          "uds_result": {"ok": False}}, "swipe"),
        ({"kind": "key", "name": "home", "uds_result": {"ok": False}}, "key"),
    ):
        try:
            lift_trace({"events": [ghost_ev]}, "/nonexistent", invoker=_fake_invoker([]))
            check(False, f"幽灵 {tag} 应 fail-closed")
        except LiftError:
            check(True, f"幽灵 {tag}（uds_result.ok=false）fail-closed")

    # 残缺 swipe（ms 非正 / 缺坐标）fail-closed
    for bad_ev, why in (
        ({"kind": "swipe", "from_preview": [1, 2], "to_preview": [3, 4], "ms": 0,
          "uds_result": {"ok": True}}, "ms 非正"),
        ({"kind": "swipe", "from_preview": [1, 2], "ms": 300,
          "uds_result": {"ok": True}}, "缺 to_preview"),
    ):
        try:
            lift_trace({"events": [bad_ev]}, "/nonexistent", invoker=_fake_invoker([]))
            check(False, f"残缺 swipe（{why}）应 fail-closed")
        except LiftError:
            check(True, f"残缺 swipe（{why}）fail-closed")

    # 键名不在硬件键封闭集内 fail-closed（"back" 是 Android 思维，iOS 没有这个键）
    try:
        lift_trace({"events": [{"kind": "key", "name": "back", "uds_result": {"ok": True}}]},
                   "/nonexistent", invoker=_fake_invoker([]))
        check(False, "未知键名应 fail-closed")
    except LiftError:
        check(True, "未知键名 fail-closed（只认 home/power/volup/voldown）")

    # 未知 kind fail-closed
    try:
        lift_trace({"events": [{"kind": "pinch", "uds_result": {"ok": True}}]},
                   "/nonexistent", invoker=_fake_invoker([]))
        check(False, "未知 kind 应 fail-closed")
    except LiftError:
        check(True, "未知 kind fail-closed（只认 tap/type/swipe/key）")

    # 缺截图证据 → 回退 tap_xy，绝不调 invoker 提升（_fake_invoker([]) 一旦被调会 StopIteration）
    miss = {"events": [{"kind": "tap", "preview": [10, 20], "native": [30, 60],
                        "before_shot": "gone.jpg", "uds_result": {"ok": True},
                        "label": "兜底"}]}
    mout = lift_trace(miss, "/nonexistent-trace-dir", invoker=_fake_invoker([]))
    check(mout["steps"][0] == {"action": "tap_xy", "px": 10, "py": 20, "label": "兜底"},
          "缺 before_shot → 回退 tap_xy（不凭空提升）")
    check(mout["decisions"][0]["reason"] == "missing_before_shot",
          "缺截图回退留痕 reason=missing_before_shot")

    # 空 trace fail-closed（拒绝产不可回放的空 recipe）
    try:
        lift_trace({"events": []}, "/nonexistent", invoker=_fake_invoker([]))
        check(False, "空 trace 应 fail-closed")
    except LiftError:
        check(True, "空 trace fail-closed（拒绝产空 recipe）")

    # 幽灵 tap（uds_result.ok != True）fail-closed——旧/手改 trace 不得提升成语义 recipe。
    # 截图存在也不行，且必须在调 invoker 之前就拦（_fake_invoker([]) 被调会 StopIteration）。
    ghost = {"events": [{"kind": "tap", "preview": [215, 617], "native": [645, 1851],
                         "before_shot": "shot_001.jpg", "uds_result": {"ok": False},
                         "label": "确认"}]}
    try:
        lift_trace(ghost, "/nonexistent", invoker=_fake_invoker([]))
        check(False, "幽灵 tap（uds_result.ok=false）应 fail-closed")
    except LiftError:
        check(True, "幽灵 tap fail-closed（不把没落地的点击提升进 recipe）")

    # ── M2 落库：提升出的 IR → operation draft（复用 AI-4 的 land_operation_draft 全套闸）──
    import yaml as _yaml
    with tempfile.TemporaryDirectory(prefix="record-lift-land-") as ld:
        ops = Path(ld) / "operations.yaml"
        ops.write_text(_yaml.safe_dump(
            {"meta": {"schema_version": 1, "app": "selftest_app", "title": "selftest ops",
                      "total": 0, "next_seq": 1}, "operations": []},
            allow_unicode=True, sort_keys=False), encoding="utf-8")
        landed = land_lifted_operation(
            "selftest_app", "enter_order_detail", "进入订单详情页",
            [{"action": "tap_text", "query": "确认"},
             {"action": "tap_xy", "px": 40, "py": 880, "label": "我的"}],
            from_ref="runs/rec/trace", ops_path=ops)
        check(landed.get("ok") is True, f"提升 IR 落库成功（{landed.get('error')}）")
        op = landed.get("operation") or {}
        check(op.get("status") == "draft", "录制产物恒 status=draft（不自行入回归库）")
        check(op.get("name") == "enter_order_detail" and str(op.get("id", "")).startswith("OP"),
              "落库分配 OP id + 保留 name")
        check((op.get("provenance") or {}).get("source") == "conversation",
              "provenance.source=conversation（§3.4 铁律①，绝不 source: ai）")
        check((op.get("provenance") or {}).get("from") == "runs/rec/trace",
              "provenance.from 记录 trace 来源")
        check(landed.get("gate", {}).get("ready") is False,
              "新 draft 转正门 not ready（无人审 / 无 passed run）")
        doc = _yaml.safe_load(ops.read_text(encoding="utf-8"))
        check(len(doc.get("operations") or []) == 1 and doc["meta"]["next_seq"] == 2,
              "操作 append 进库 + next_seq 递增 + 整库贴 operation.v1")
        # active 重名 fail-closed（draft 允许多次迭代，active name 必须唯一）
        doc["operations"][0]["status"] = "active"
        ops.write_text(_yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        again = land_lifted_operation("selftest_app", "enter_order_detail", "重名",
                                      [{"action": "tap_text", "query": "x"}], ops_path=ops)
        check(again.get("ok") is False and "active 操作重名" in (again.get("error") or ""),
              "active 重名操作 fail-closed（拒绝写脏）")
        # 瞎编 verb fail-closed（IR dispatcher 静态闸拦在落库前）
        bogus = land_lifted_operation("selftest_app", "bogus_op", "瞎编",
                                      [{"action": "frobnicate", "x": 1}], ops_path=ops)
        check(bogus.get("ok") is False, "瞎编 verb 不过 validate_recipe（落库前 fail-closed）")
        # 非法 provenance（未知 source）落库前就拦，且**绝不写脏库**（审查 P2：原先 append 后才发现）
        from autophone.application.ai_draft import land_operation_draft
        before = _yaml.safe_load(ops.read_text(encoding="utf-8"))
        dirty = land_operation_draft(
            "selftest_app", {"name": "bad_prov", "summary": "x",
                             "steps": [{"action": "tap_text", "query": "x"}]},
            provenance={"source": "ai"}, ops_path=ops)  # source: ai 不在 enum
        after = _yaml.safe_load(ops.read_text(encoding="utf-8"))
        check(dirty.get("ok") is False, "非法 provenance fail-closed")
        check(len(after.get("operations") or []) == len(before.get("operations") or [])
              and after["meta"]["next_seq"] == before["meta"]["next_seq"],
              "非法 provenance 不污染库（操作数 / next_seq 不变）")

    print(f"\nselftest: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="record_lift",
        description="M1: 把 record_capture trace 提升成语义 typed IR（tap_xy → tap_text，判不准回退）")
    ap.add_argument("--trace", help="trace.json 路径或其所在目录")
    ap.add_argument("--out", help="输出 recipe YAML 路径（省略则打印到 stdout）")
    ap.add_argument("--id", default=None, help="recipe id（写进 YAML，便于追溯）")
    ap.add_argument("--provider", default=None, help="claude|codex|auto（默认 env/codex）")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=120.0, help="每次 AI 调用超时秒")
    ap.add_argument("--accept-confidence", default=",".join(DEFAULT_ACCEPT),
                    help="接受提升的置信档（逗号分隔，默认 high,medium）")
    ap.add_argument("--screens", default=None,
                    help="可选 screens/<build>.yaml：分类 before_shot 并输出 screen_expectations sidecar")
    ap.add_argument("--screen-statuses", default="active",
                    help="screen 分类使用的状态集合（逗号分隔，默认 active）")
    ap.add_argument("--land", metavar="APP",
                    help="把提升出的 IR 落库为该 app 的 operation draft（status=draft，走 AI-4 同款落库闸）")
    ap.add_argument("--name", help="--land 时必填：操作的 snake_case 稳定语义名")
    ap.add_argument("--summary", help="--land 时必填：一句话操作摘要")
    ap.add_argument("--ops-path", default=None,
                    help="操作库 YAML 路径覆写（默认取 app profile 的 operations_path；测试用）")
    ap.add_argument("--selftest", action="store_true",
                    help="无 VM/无模型契约检查（假 invoker）")
    args = ap.parse_args(argv)

    if args.selftest:
        return _run_selftest()
    if not args.trace:
        ap.error("--trace 必填（除 --selftest）")
    if args.land and not (args.name and args.summary):
        ap.error("--land 需同时给 --name 与 --summary（操作的稳定语义名 + 一句摘要）")

    accept = tuple(c.strip() for c in args.accept_confidence.split(",") if c.strip())
    screen_statuses = tuple(c.strip() for c in args.screen_statuses.split(",") if c.strip())
    out = lift_trace_file(args.trace, provider=args.provider, model=args.model,
                          timeout_s=args.timeout, accept_confidence=accept,
                          screen_asset_path=args.screens, screen_statuses=screen_statuses)
    print(json.dumps(out["summary"], ensure_ascii=False))
    for d in out["decisions"]:
        tag = "✓lift" if d["source"] == "ai" else "·xy  "
        extra = f" → tap_text({d['ai_text']!r})" if d["source"] == "ai" else f" [{d['reason']}]"
        print(f"  [{d['index']}] {tag}{extra}")

    recipe_doc: dict[str, Any] = {"steps": out["steps"]}
    if out.get("screen_starts_with"):
        recipe_doc["screen_starts_with"] = out["screen_starts_with"]
    if out.get("screen_expectations"):
        recipe_doc["screen_expectations"] = out["screen_expectations"]
    if args.id:
        recipe_doc = {"id": args.id, **recipe_doc}
    if args.out:
        import yaml
        Path(args.out).write_text(
            yaml.safe_dump(recipe_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        report = Path(args.out).with_suffix(".lift-report.json")
        report.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"recipe: {args.out}")
        print(f"report: {report}")
    else:
        import yaml
        print(yaml.safe_dump(recipe_doc, allow_unicode=True, sort_keys=False))

    if args.land:
        res = land_lifted_operation(
            args.land, args.name, args.summary, out["steps"],
            from_ref=str(args.trace), provider=args.provider, model=args.model,
            ops_path=Path(args.ops_path) if args.ops_path else None)
        if not res.get("ok"):
            print(f"land draft failed: {res.get('error')}", file=sys.stderr)
            return 1
        op = res["operation"]
        print(f"landed operation draft: {op['id']} {op['name']} (status=draft, r{op['revision']})")
        print(f"  registry: {res['path']}")
        print(f"  gate: {json.dumps(res['gate'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
