"""Shared helpers for case importers.

These helpers are app-agnostic. Workbook-specific parsing stays in dedicated
importers; generic template importers can depend on this module directly.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# These keywords decide the initial automation class. The result is a
# reviewable seed value, not authoritative case truth.
BLOCKED_KEYWORDS = [
    "扫码", "扫一扫", "上岗码", "离岗码", "小休码",
    "验证码",
    "实名",
    "检查更新",
]
FIXTURE_KEYWORDS = [
    "取餐", "送餐", "送达", "到店", "返店", "离店",
    "订单", "排班", "被上岗", "被离岗", "被小休",
    "健康填报", "健康信息",
    "业绩", "当前订单",
]

_STEP_NUM = re.compile(r"^\s*[（(]?\d+\s*[\.\)、）]\s*")

# 通用模板导入时的轻量语义归纳。它刻意是确定性、可审阅的：只给详情页把“原文”
# 组织成前置状态 / 交互 / 检查点，不会凭空生成可执行 recipe，也不会改变 runner 消费的
# precondition/steps/expects。真正的执行步骤仍须由人工或 AI recipe 起草后复核。
_TAP_PREFIX = re.compile(r"^(?:请)?(?:点击|点按|单击)\s*")
_NAVIGATE_PREFIX = re.compile(r"^(?:请)?(?:打开|进入|前往|返回到?)\s*")
_INPUT_PREFIX = re.compile(r"^(?:请)?(?:输入|填写|录入)\s*")
_SELECT_PREFIX = re.compile(r"^(?:请)?(?:选择|勾选|切换)\s*")
_WAIT_PREFIX = re.compile(r"^(?:请)?(?:等待|稍候)\s*")
_CHECK_PREFIX = re.compile(r"^(?:请)?(?:查看|校验|检查|确认)\s*")
_PAREN_SUFFIX = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")


def _semantic_precondition_kind(label: str) -> str:
    text = label.lower()
    if any(token in text for token in ("已登录", "登录态", "登录状态", "未登录", "登出")):
        return "login_state"
    if any(token in text for token in ("订单", "取餐", "送餐", "到店", "离店", "配送")):
        return "order_state"
    if any(token in text for token in ("定位", "距离", "门店", "店铺", "地址")):
        return "location"
    if any(token in text for token in ("安装", "启动", "前台", "app")):
        return "app_state"
    return "other"


def _semantic_action(label: str) -> dict[str, str]:
    """把一条人读步骤归纳成展示用 {kind,target,label}，原句仍由 label 保留。"""
    for kind, prefix in (
        ("tap", _TAP_PREFIX),
        ("navigate", _NAVIGATE_PREFIX),
        ("input", _INPUT_PREFIX),
        ("select", _SELECT_PREFIX),
        ("wait", _WAIT_PREFIX),
        ("check", _CHECK_PREFIX),
    ):
        if prefix.search(label):
            target = prefix.sub("", label).strip()
            # “点击【确认到店】（订单列表）”的目标是确认到店；括号保留在 label 以便回溯入口语境。
            target = _PAREN_SUFFIX.sub("", target).strip().strip("【】[]")
            return {"kind": kind, "target": target or label, "label": label}
    return {"kind": "other", "target": label, "label": label}


def _semantic_assertion_kind(label: str) -> str:
    if any(token in label for token in ("成功", "失败", "消失", "关闭", "跳转", "变为", "仍在", "更新")):
        return "state_change"
    if any(token in label for token in ("出现", "展示", "显示", "可见", "提示")):
        return "visibility"
    return "other"


def derive_import_semantics(
    *,
    item: str,
    precondition: list[str],
    steps: list[str],
    expects: list[str],
    source: str = "import",
) -> dict[str, Any]:
    """生成用例详情右栏消费的导入解析，不替代原文或可执行 recipe。

    ``source=import`` 用于新导入、会随 YAML 留存；``source=derived`` 供 API 给历史
    用例即时补视图，避免旧数据必须重导才看到组织效果。
    """
    preconditions = [
        {"kind": _semantic_precondition_kind(label), "label": label}
        for label in precondition
        if label
    ]
    actions = [_semantic_action(label) for label in steps if label]
    # “取消 / 确认”经常是同一确认框的两条分支，给右栏一个显式分支提示；不推断
    # 断言归属，也绝不改动原始步骤顺序。
    for action in actions:
        if action["kind"] != "tap":
            continue
        target = action["target"].replace(" ", "")
        if target in {"取消", "关闭", "暂不"}:
            action["branch"] = "cancel"
        elif target in {"确认", "确定", "继续"}:
            action["branch"] = "confirm"
    assertions = [
        {"kind": _semantic_assertion_kind(label), "label": label}
        for label in expects
        if label
    ]
    name = item or "本用例"
    input_fingerprint = hashlib.sha256(
        json.dumps(
            {"precondition": precondition, "steps": steps, "expects": expects},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "version": 1,
        "source": source,
        # 用例内容被人工编辑后，API 用它识别旧的导入解析并即时重新归纳，避免右栏展示陈旧结果。
        "input_fingerprint": input_fingerprint,
        "summary": f"{name}：{len(preconditions)} 个前置状态、{len(actions)} 个交互、{len(assertions)} 个检查点",
        "preconditions": preconditions,
        "actions": actions,
        "assertions": assertions,
    }


def classify_automation_match(text: str) -> dict[str, str | None]:
    """Return ``{automation, reason, keyword}`` for the first matching keyword.

    ``classify_automation`` 与导入预览的准备度建议共用本函数，命中顺序与结果相同；
    ``keyword`` 是命中的关键词，未命中时为 ``None``。
    """
    for kw in BLOCKED_KEYWORDS:
        if kw in text:
            return {"automation": "blocked_by_infra", "reason": f"命中地基依赖关键词「{kw}」", "keyword": kw}
    for kw in FIXTURE_KEYWORDS:
        if kw in text:
            return {"automation": "needs_fixture", "reason": f"命中预置条件关键词「{kw}」", "keyword": kw}
    return {"automation": "", "reason": "自动化状态待人工评估", "keyword": None}


def classify_automation(text: str) -> tuple[str, str]:
    """Return a safe initial automation class and its human-reviewable reason.

    关键词只能证明已知阻塞/夹具需求；未命中不能证明用例已满足
    ``ui_ready`` 准入契约，因此保持空值等待人工复核。
    """
    match = classify_automation_match(text)
    return (str(match["automation"]), str(match["reason"]))


def norm_cell(value: Any) -> str:
    """Normalize a spreadsheet cell into trimmed text; None becomes empty."""
    if value is None:
        return ""
    return str(value).strip()


def split_numbered(text: str) -> list[str]:
    """Split numbered multiline step/expectation text into an ordered list."""
    text = text.strip()
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1 and _STEP_NUM.search(text):
        parts = re.split(r"(?=[（(]?\d+\s*[\.\)、）])", text)
        lines = [p.strip() for p in parts if p.strip()]
    return [_STEP_NUM.sub("", ln).strip() for ln in lines]


def patch_meta_scalar(text: str, key: str, value: Any) -> str:
    """Patch a scalar field under top-level YAML ``meta`` without reformatting."""
    pat = re.compile(rf"^(  {re.escape(key)}: ).*$", re.MULTILINE)
    new, n = pat.subn(lambda m: f"{m.group(1)}{value}", text, count=1)
    if n == 0:
        raise ValueError(f"meta.{key} 未找到，无法定点 patch")
    return new


# Backward-compatible private aliases for importers that have not moved yet.
_norm_cell = norm_cell
_patch_meta_scalar = patch_meta_scalar
