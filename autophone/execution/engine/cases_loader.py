"""cases_loader.py — cases YAML 加载（PyYAML 可选，降级时 fail-loud）。

phase2 runner（VM smoke 路径）与 phase5 coverage 共用这一份 fallback 解析器，
避免两份拷贝静默分歧（review d4c4124：两处 wanted 字段集已经不一致）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# fallback 只保留这些扁平标量字段（选例与报告所需）。
# steps/expects/precondition/suitable/raw 等嵌套结构会被丢弃——load_cases 会打降级警告。
FALLBACK_FIELDS = {
    "row", "module", "item", "order_type", "automation",
    "automation_reason", "level", "status", "revision", "login_precondition",
    "business_family", "family_id", "pair_id", "family_variant",
}

# 这些结构化字段的内容（操作引用列表）降级解析读不出，但它们的**存在性**不能丢：
# run_case 的 fail-closed 闸靠 case.get("setup")/get("teardown") 拦截「配了前后置但执行链
# 未接入」的用例。若 fallback 把它们整个吞掉，带 setup 的用例会被当成「无前后置」继续跑
# recipe/script → 退回假 pass（review 547312e 续审 P1）。所以这里只记一个非空哨兵让闸触发。
FALLBACK_PRESENCE_FIELDS = {"setup", "teardown"}
_FALLBACK_PRESENCE_MARK = [{"op": "<unparsed-in-fallback>"}]


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "''", '""'}:
        return ""
    if value in {"null", "Null", "NULL", "~"}:
        return None
    # len>=2 防护：单个引号字符（如 `'`）首尾是同一字符，切片会得到空串
    if len(value) >= 2 and (
        (value.startswith("'") and value.endswith("'"))
        or (value.startswith('"') and value.endswith('"'))
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _load_cases_fallback(path: Path) -> list[dict[str, Any]]:
    """无 PyYAML 时的降级解析：只读 FALLBACK_FIELDS 中的扁平标量字段。

    Base host Python on this machine does not always have PyYAML. The VM smoke
    path should still be able to run known UI-ready cases such as R21, so this
    parser handles the flat scalar fields used for selection and reporting.
    """
    cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("- id:"):
            if current:
                cases.append(current)
            current = {"id": _parse_scalar(raw.split(":", 1)[1])}
            continue
        if current is None or not raw.startswith("  "):
            continue
        stripped = raw.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key in FALLBACK_FIELDS:
            current[key] = _parse_scalar(value)
        elif key in FALLBACK_PRESENCE_FIELDS and value.strip() not in {"[]", "{}"}:
            # block 形态（items 在后续行，value 为空）或内联非空 → 视为已配置，记哨兵让 run_case
            # 闸触发 fail-closed。内联空（[]/{}）= 无配置，不标记。
            current[key] = list(_FALLBACK_PRESENCE_MARK)
    if current:
        cases.append(current)
    return cases


def load_cases(path: Path) -> list[dict[str, Any]]:
    if yaml is None:
        # 降级必须 loud：fallback 与 yaml.safe_load 的解析结果不同
        # （嵌套字段丢失、true/false 不转 bool），不能让消费方无感知。
        logger.warning(
            "PyYAML 缺失，降级解析 %s；只保留扁平字段 %s，嵌套字段被丢弃。完整解析请先 `uv sync`。",
            path, sorted(FALLBACK_FIELDS),
        )
        return _load_cases_fallback(path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(doc.get("cases", []) or [])
