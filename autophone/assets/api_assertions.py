"""Pure-kernel static API assertion grammar & formula syntax validation.

Shared single source of truth used by both:
- ``autophone.assets.entity_binding`` (kernel static contract validation)
- ``autophone.observation.frida.api_oracle`` (domain runtime API oracle evaluation)

Contains zero Frida, observation, or execution dependencies so ``autophone-kernel``
can validate ``entity-binding.v1.json`` API assertion declarations standalone.
"""
from __future__ import annotations

import ast
import re
from typing import Any

_FILTER_TERM_RE = re.compile(r"\s*@\.([\w.]+)\s*(==|!=)\s*(.+?)\s*$")

_FORMULA_NODE_TYPES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Load,
)

# 跨元素关系断言原语（§六.needs_manual 收编）。除第二份半价/赠品关联/计算型金额外，
# 点餐类用例批量 review 还暴露了数组优惠分摊求和、字段大小关系和「还差 ¥X」文本阈值三类。
CROSS_ELEMENT_OPS = frozenset({
    "half_price_pair", "gift_relation", "computed_amount",
    "aggregate_formula", "numeric_relation", "text_amount_formula",
})
ALLOWED_OPS = frozenset({"eq", "ne", "empty", "exists", "contains", "present", "regex"}) | CROSS_ELEMENT_OPS


def _lit(v: str) -> Any:
    """过滤谓词右值字面量：去引号 / bool / 数字 / 其余原样（${var} 已在外层替换）。"""
    v = v.strip()
    if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
        return v[1:-1]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _parse_filter(expr: str) -> list[tuple[str, str, Any]]:
    """?(...) 内的合取谓词：@.f=='v' [&& @.g==h]。返回 [(field, op, value)]。"""
    terms: list[tuple[str, str, Any]] = []
    for part in expr.split("&&"):
        m = _FILTER_TERM_RE.match(part)
        if not m:
            raise ValueError(f"不支持的过滤谓词: {part!r}（子集只支持 @.f==v / @.f!=v，&& 合取）")
        field, op, val = m.groups()
        terms.append((field, op, _lit(val)))
    return terms


def _parse_path(path: str) -> list[tuple[str, Any]]:
    """把 JSONPath 字符串解析成段序列：('key',s)|('index',n)|('wild',None)|('filter',terms)。"""
    segs: list[tuple[str, Any]] = []
    i, n, buf = 0, len(path), ""
    while i < n:
        c = path[i]
        if c == ".":
            if buf:
                segs.append(("key", buf))
                buf = ""
            i += 1
        elif c == "[":
            if buf:
                segs.append(("key", buf))
                buf = ""
            j = path.find("]", i)
            if j < 0:
                raise ValueError(f"路径 {path!r} 有未闭合的 [")
            inner = path[i + 1:j].strip()
            if inner == "*":
                segs.append(("wild", None))
            elif inner.startswith("?(") and inner.endswith(")"):
                segs.append(("filter", _parse_filter(inner[2:-1])))
            elif inner.lstrip("-").isdigit():
                segs.append(("index", int(inner)))
            elif len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0]:
                segs.append(("key", inner[1:-1]))
            else:
                segs.append(("key", inner))
            i = j + 1
        else:
            buf += c
            i += 1
    if buf:
        segs.append(("key", buf))
    return segs


def _spec_op(spec: Any) -> str:
    """assert 值 → op 名（不做 ${var} 替换，纯静态形态判定）。对象形取 op；标量/魔法串走旧口径。"""
    if isinstance(spec, dict):
        return str(spec.get("op", "eq"))
    if spec == "*present*":
        return "present"
    if isinstance(spec, str) and spec.startswith("*regex:"):
        return "regex"
    return "eq"


def _validate_named_lookup(key: str, prefix: str, label: str) -> None:
    sub = key[len(prefix):]
    if not sub or sub != sub.strip() or any(ch.isspace() for ch in sub):
        raise ValueError(f"assert 键 {key!r} 非法：{label} 名称不能为空或包含空白")


def static_check_assert_key(key: str) -> None:
    """静态校验一条 assert 的 path 形态（§六「JSONPath 过不了静态校验 → fail-closed」）。

    合法：`status` | `resp_error` | `req.query.<param>` | `req.header.<name>` |
    `resp.header.<name>` | `resp.<path>` | `req.<path>`（body. 前缀可省）。
    <path> 走 _parse_path，语法错（未闭合 [ / 不支持的过滤谓词）即抛 ValueError。
    不解析 ${var}（降解期字面校验）。
    """
    if key in ("status", "resp_error"):
        return
    if key.startswith("req.query."):
        _validate_named_lookup(key, "req.query.", "query 参数")
        return
    if key.startswith("req.header."):
        _validate_named_lookup(key, "req.header.", "req header")
        return
    if key.startswith("resp.header."):
        _validate_named_lookup(key, "resp.header.", "resp header")
        return
    for side in ("resp", "req"):
        prefix = f"{side}."
        if key.startswith(prefix):
            sub = key[len(prefix):]
            if sub.startswith("body."):
                sub = sub[len("body."):]
            if not sub:
                raise ValueError(f"assert 键 {key!r} 非法：{side} 路径不能为空")
            _parse_path(sub)  # 语法错抛 ValueError
            return
    raise ValueError(
        f"assert 键 {key!r} 非法：须为 status / resp_error / req.query.<param> / "
        "req.header.<name> / resp.header.<name> / resp.<path> / req.<path>"
    )


def _eval_formula(formula: str, values: dict[str, float]) -> float:
    """安全求值一个只含 +-*/() 与已声明 refs 名的算术表达式（不经 eval()，AST 白名单遍历）。"""
    tree = ast.parse(formula, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _FORMULA_NODE_TYPES):
            raise ValueError(f"formula 含不支持的语法节点 {type(node).__name__}（只允许 + - * / () 与 refs 名）")
        if isinstance(node, ast.Name) and node.id not in values:
            raise ValueError(f"formula 引用了未在 refs 声明的名字: {node.id!r}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("formula 只允许数值常量")

    def _ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            v = _ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            left, right = _ev(node.left), _ev(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        raise ValueError(f"formula 不支持的节点: {node!r}")

    return _ev(tree)


def _static_check_cross_op(where: str, op: str, spec: dict) -> None:
    """跨元素 op 的结构闸：缺必填键 / 类型不对 / formula 语法非法 → 抛 ValueError（fail-closed）。"""
    if op == "half_price_pair":
        for f in ("price_field", "base_field", "factors"):
            if f not in spec:
                raise ValueError(f"{where} half_price_pair 缺必填键 {f!r}")
        if not isinstance(spec["factors"], list) or not spec["factors"]:
            raise ValueError(f"{where} half_price_pair.factors 须为非空数组（每元素的倍率，按位对齐）")
    elif op == "gift_relation":
        for f in ("gift_filter", "link_field", "target_field"):
            if f not in spec:
                raise ValueError(f"{where} gift_relation 缺必填键 {f!r}")
        if not isinstance(spec["gift_filter"], dict) or not spec["gift_filter"]:
            raise ValueError(f"{where} gift_relation.gift_filter 须为非空对象（{{字段: 值}} 定位赠品元素）")
    elif op == "computed_amount":
        refs, formula = spec.get("refs"), spec.get("formula")
        if not isinstance(refs, dict) or not refs:
            raise ValueError(f"{where} computed_amount 缺 refs（{{名字: JSONPath}}，供 formula 引用）")
        if not isinstance(formula, str) or not formula.strip():
            raise ValueError(f"{where} computed_amount 缺 formula（如 'origin - display'）")
        for _name, path in refs.items():
            static_check_assert_key(str(path))
        try:
            _eval_formula(formula, {n: 0.0 for n in refs})
        except Exception as e:
            raise ValueError(f"{where} computed_amount.formula 非法: {e}") from e
    elif op == "aggregate_formula":
        refs, formula = spec.get("refs"), spec.get("formula")
        if not isinstance(refs, dict) or not refs:
            raise ValueError(f"{where} aggregate_formula 缺 refs")
        if not isinstance(formula, str) or not formula.strip():
            raise ValueError(f"{where} aggregate_formula 缺 formula")
        for _name, path in refs.items():
            static_check_assert_key(str(path))
        try:
            _eval_formula(formula, {str(name): 0.0 for name in refs})
        except Exception as e:
            raise ValueError(f"{where} aggregate_formula.formula 非法: {e}") from e
    elif op == "numeric_relation":
        refs, relation = spec.get("refs"), spec.get("relation")
        if not isinstance(refs, dict) or not refs:
            raise ValueError(f"{where} numeric_relation 缺 refs")
        if relation not in {"gt", "ge", "lt", "le", "eq_min"}:
            raise ValueError(f"{where} numeric_relation.relation 非法")
        if relation == "eq_min" and len(refs) < 2:
            raise ValueError(f"{where} numeric_relation eq_min 至少需要两个 refs")
        if relation != "eq_min" and len(refs) != 1:
            raise ValueError(f"{where} numeric_relation {relation} 需要恰好一个 ref")
        for _name, path in refs.items():
            static_check_assert_key(str(path))
    elif op == "text_amount_formula":
        for field in ("pattern", "capture", "refs", "formula", "expected"):
            if field not in spec:
                raise ValueError(f"{where} text_amount_formula 缺 {field}")
        refs, capture, formula = spec["refs"], spec["capture"], spec["formula"]
        if not isinstance(refs, dict) or not isinstance(capture, str) or not capture:
            raise ValueError(f"{where} text_amount_formula refs/capture 非法")
        if capture in refs or not isinstance(formula, str) or not formula.strip():
            raise ValueError(f"{where} text_amount_formula formula/capture 非法")
        try:
            compiled = re.compile(str(spec["pattern"]))
        except re.error as e:
            raise ValueError(f"{where} text_amount_formula.pattern 非法: {e}") from e
        if capture not in compiled.groupindex:
            raise ValueError(f"{where} text_amount_formula.pattern 必须含命名捕获组 {capture!r}")
        for _name, path in refs.items():
            static_check_assert_key(str(path))
        try:
            _eval_formula(formula, {**{str(name): 0.0 for name in refs}, capture: 0.0})
        except Exception as e:
            raise ValueError(f"{where} text_amount_formula.formula 非法: {e}") from e


__all__ = [
    "ALLOWED_OPS",
    "CROSS_ELEMENT_OPS",
    "_FILTER_TERM_RE",
    "_FORMULA_NODE_TYPES",
    "_eval_formula",
    "_lit",
    "_parse_filter",
    "_parse_path",
    "_spec_op",
    "_static_check_cross_op",
    "_validate_named_lookup",
    "static_check_assert_key",
]
