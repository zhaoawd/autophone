#!/usr/bin/env python3
# coding=utf8
"""断言层：合并后的 capture JSONL × api_expectations.yaml → 三态结果。

规则数据驱动，三态评估（pass/fail/skip），结果汇入 HTML 报告。
对某个 (case_id[, operation/step]) 窗口内归因到的调用，逐条评估期望：

  - expect 缺失（该发的调用没发）          → fail(missing)
  - expect 命中但 status/字段不符          → fail(assert)
  - forbid 命中（不该出现的调用出现了）    → fail(unexpected)
  - ${var} 在 fixture 上下文里解析不到      → fail(unresolved_var)（fail-closed：
    不能拿 "${order_id}" 字面量去比对产生误导性结果，也不能让 forbid 因 glob
    永不匹配而静默放行）
  - 全满足                                  → pass
  - 窗口内无任何调用且 expect 非空          → fail(missing)（继承「绿条不能是啥都没跑」）

字段取值：JSONPath 风格点路径，在 req_body_text / resp_body_text（解析成 JSON）上解；
`${var}` 用 fixture 上下文（造单回传的 order_id 等）模板替换。
易变字段（sign/time）只在 assert 里用 `*present*` / `*regex:*` 校验存在性/格式，不硬比对值（§九）。

CLI（拿一份 capture.jsonl 对某 case 跑断言）：
    uv run python -m autophone.observation.frida.api_oracle <capture.jsonl> <CASE_ID> [--exp path] [--fixture-context path]
"""
from __future__ import annotations

from collections.abc import Mapping
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from autophone.assets.api_assertions import (
    ALLOWED_OPS,
    CROSS_ELEMENT_OPS,
    _FILTER_TERM_RE,
    _FORMULA_NODE_TYPES,
    _eval_formula,
    _lit,
    _parse_filter,
    _parse_path,
    _spec_op,
    _static_check_cross_op,
    _validate_named_lookup,
    static_check_assert_key,
)

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # base python in this repo may not have PyYAML
    yaml = None  # type: ignore[assignment]

DEFAULT_EXPECTATIONS = Path(__file__).resolve().parent / "cases" / "api_expectations.yaml"


def normalize_expectations(doc: Any, *, statuses: tuple[str, ...] | None = ("active",)) -> dict[str, Any]:
    """把断言资产文档归一成 evaluate_case 吃的 `{CASE_ID: {expect,forbid}}` 映射。

    两种形态自动辨识：
      - **list 形**（生命周期资产，§七）：`meta + expectations:[{id,status,expect,forbid}]`，
        按 `statuses` 过滤（默认只留 active——运行期回归只读 active）；statuses=None 不过滤。
      - **legacy map 形**（旧全局文件）：顶层直接 `CASE_ID → {expect,forbid}`，无 status 概念，
        原样透传（仅剔除 meta 键）。
    """
    if not isinstance(doc, dict):
        return {}
    exps = doc.get("expectations")
    if isinstance(exps, list):
        out: dict[str, Any] = {}
        for e in exps:
            if not isinstance(e, dict):
                continue
            if statuses is not None and e.get("status") not in statuses:
                continue
            cid = str(e.get("id") or "")
            if not cid:
                continue
            out[cid] = {k: e[k] for k in ("expect", "forbid") if k in e}
        return out
    return {k: v for k, v in doc.items() if k != "meta"}


def load_expectations(path: Path = DEFAULT_EXPECTATIONS, *,
                      statuses: tuple[str, ...] | None = ("active",)) -> dict[str, Any]:
    """读断言资产文件 → `{CASE_ID: {expect,forbid}}`。list 形按 `statuses` 过滤，map 形透传。"""
    if not path.exists():
        return {}
    if path.suffix.lower() == ".json":
        doc = json.loads(path.read_text(encoding="utf-8"))
    else:
        if yaml is None:
            raise RuntimeError(
                f"PyYAML is required to read expectations from {path}; "
                "use a JSON expectations file or run `uv sync`"
            )
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return normalize_expectations(doc, statuses=statuses)


def load_app_expectations(app: str, *, statuses: tuple[str, ...] | None = ("active",)) -> dict[str, Any]:
    """按 app 读 per-app 断言资产（`apps/<app>/api_expectations.yaml`）。缺文件 → {}。

    延迟 import autophone.platform（autophone.observation.frida 保持对平台层零静态依赖）。
    """
    from autophone.assets.apps import load_app_profile
    return load_expectations(load_app_profile(app).api_expectations_path(), statuses=statuses)


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_key(value: Any) -> str:
    return re.sub(r"\W+", "_", str(value)).strip("_")


def _put_var(out: dict[str, Any], key: str, value: Any) -> None:
    key = _safe_key(key)
    if key and _scalar(value) and value is not None:
        out[key] = value


def _flatten_vars(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}_{_safe_key(key)}" if prefix else _safe_key(key)
            if _scalar(item):
                _put_var(out, next_prefix, item)
            else:
                _flatten_vars(next_prefix, item, out)


def fixture_context_vars(fixture_context: Any | None) -> dict[str, Any]:
    """Build `${var}` substitutions from a Phase 5 FixtureContext.

    Accepted inputs: a FixtureContext-like object with `to_dict()`, a dict, or a
    JSON path. Variable names are intentionally flat because expectation YAML
    uses `${word}` placeholders. Common aliases (`order_id`, `actor_phone`,
    `store_code`, etc.) are provided in addition to prefixed forms.
    """
    if fixture_context is None:
        return {}
    if isinstance(fixture_context, (str, Path)):
        data = json.loads(Path(fixture_context).read_text(encoding="utf-8"))
    elif hasattr(fixture_context, "to_dict") and callable(fixture_context.to_dict):
        data = fixture_context.to_dict()
    elif isinstance(fixture_context, dict):
        data = fixture_context
    else:
        raise TypeError(f"unsupported fixture context: {type(fixture_context).__name__}")
    if not isinstance(data, dict):
        raise ValueError("fixture context root must be an object")

    out: dict[str, Any] = {}
    _put_var(out, "fixture_id", data.get("fixture_id"))
    _put_var(out, "case_id", data.get("case_id"))
    _put_var(out, "fixture_status", data.get("status"))
    _put_var(out, "env", data.get("env"))

    order = data.get("order") or {}
    actor = data.get("actor") or {}
    if isinstance(order, dict):
        _flatten_vars("order", order, out)
        for key in (
            "order_id", "initial_state", "distance_band",
            "brand", "store_code", "shop_id", "customer_latitude",
            "customer_longitude", "store_latitude", "store_longitude",
        ):
            _put_var(out, key, order.get(key))
        _put_var(out, "order_type", order.get("type"))
    if isinstance(actor, dict):
        _flatten_vars("actor", actor, out)
        _put_var(out, "actor_phone", actor.get("phone"))
        _put_var(out, "actor_state", actor.get("state"))
        _put_var(out, "login_state", actor.get("login_state"))
    return out


def fixture_context_vars_from_env(env_name: str = "AP_FIXTURE_CONTEXT") -> dict[str, Any]:
    path = os.environ.get(env_name)
    return fixture_context_vars(Path(path)) if path else {}


# ── 通用扁平 fixture-context 装载器 ─────────────────────────────────────────
# fixture_context_vars 读取 order/actor 嵌套形状；其他用例的外部真值
# （threshold/discount_amount…）是「CASE_ID → {var: 标量}」的扁平真值表，形状不同。
# 本装载器按 case_id 取子 dict、逐个当 ${var}（只收标量），与嵌套形状装载器互不依赖。

def flat_fixture_vars(fixtures: Any | None, case_id: str) -> dict[str, Any]:
    """扁平真值表 `{CASE_ID: {var: value}}` → 该 case 的 `${var}` 上下文（仅标量）。

    fixtures 可为：dict、JSON/YAML 路径、或 None（→ {}）。解析不到该 case 的子表即空 dict，
    最终经 evaluate_case 的 _unresolved_vars 兜底为 fail(unresolved_var)，不静默放行。
    """
    if fixtures is None:
        return {}
    if isinstance(fixtures, (str, Path)):
        p = Path(fixtures)
        if not p.exists():
            return {}
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".json":
            fixtures = json.loads(text)
        else:
            if yaml is None:
                raise RuntimeError(f"PyYAML required to read flat fixtures from {p}")
            fixtures = yaml.safe_load(text) or {}
    if not isinstance(fixtures, dict):
        raise ValueError("flat fixtures root must be an object keyed by CASE_ID")
    sub = fixtures.get(case_id) or fixtures.get(str(case_id)) or {}
    if not isinstance(sub, dict):
        raise ValueError(f"flat fixtures[{case_id}] must be an object of var→value")
    out: dict[str, Any] = {}
    for key, value in sub.items():
        _put_var(out, key, value)
    return out


def load_app_fixtures(app: str, case_id: str) -> dict[str, Any]:
    """按 app 读扁平真值表（`apps/<app>/api_fixtures.yaml`）里该 case 的 `${var}` 上下文。"""
    from autophone.assets.apps import load_app_profile
    return flat_fixture_vars(load_app_profile(app).api_fixtures_path(), case_id)


_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\}")

#: Post-body housekeeping marks whose captured calls must never leak into case interaction windows.
POST_BODY_HOUSEKEEPING_STEPS: frozenset[str] = frozenset({
    "body_end",
    "post_case_cleanup",
    "entity_readback",
})


def _subst(value: Any, ctx: Mapping[str, Any]) -> Any:
    """${var} / ${entity.<attr>} 模板替换（仅对 str）。ctx 来自 fixture 或 entity_binding。

    解析不到的变量保留字面量；调用前由 evaluate_case / evaluate_entity_api_assertions
    经 _unresolved_vars 整体拦截为 fail(unresolved_var)，不会拿字面量参与比对。
    """
    if isinstance(value, str):
        def repl(m):
            return str(ctx.get(m.group(1), m.group(0)))
        return _VAR_RE.sub(repl, value)
    return value


def _unresolved_vars(obj: Any, ctx: Mapping[str, Any]) -> list[str]:
    """递归收集 obj（expect/forbid 条目）里 ctx 解析不到的 ${var} 名。"""
    missing: set[str] = set()
    if isinstance(obj, str):
        missing.update(n for n in _VAR_RE.findall(obj) if n not in ctx)
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            missing.update(_unresolved_vars(k, ctx))
            missing.update(_unresolved_vars(v, ctx))
    elif isinstance(obj, list):
        for v in obj:
            missing.update(_unresolved_vars(v, ctx))
    return sorted(missing)


def _body_json(call: dict, side: str) -> Any:
    """req/resp body_text → JSON 对象；无正文或无法解析时返回 None。"""
    txt = call.get(f"{side}_body_text")
    if txt:
        try:
            return json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _without_same_action_double_body_shadows(calls: list[dict]) -> list[dict]:
    """忽略同一 Action 的已知 wrapper 双采集影子，保留原始 capture 不变。

    部分 App 的 completion/delegate wrapper 可能为同一 ``action_id`` 额外生成一条
    ``JSON + JSON`` 响应。只有同一 Action 与响应元数据组内同时存在可解析的
    单份正文，且请求正文相同，才丢弃双份记录。已知 stream 影子不保留请求正文，
    仅在 ``req_body_len == -1`` 且 canonical 保留请求正文时允许匹配。缺少
    canonical 记录或跨 Action/请求的不可解析响应继续保留并 fail-closed。
    """
    key_fields = (
        "case_id",
        "action_id",
        "operation",
        "step",
        "method",
        "url",
        "status",
        "subject_epoch_ref",
    )

    def key(call: dict) -> tuple[str, ...]:
        return tuple(str(call.get(field) or "") for field in key_fields)

    bodies: dict[tuple[str, ...], dict[str, list[dict]]] = {}
    for call in calls:
        if not call.get("action_id"):
            continue
        text = call.get("resp_body_text")
        if isinstance(text, str) and text:
            bodies.setdefault(key(call), {}).setdefault(text, []).append(call)

    def request_matches(shadow: dict, canonical: dict) -> bool:
        shadow_request = shadow.get("req_body_text")
        canonical_request = canonical.get("req_body_text")
        if isinstance(shadow_request, str) and shadow_request:
            return shadow_request == canonical_request
        return (
            str(shadow.get("req_body_len")) == "-1"
            and isinstance(canonical_request, str)
            and bool(canonical_request)
        )

    kept: list[dict] = []
    for call in calls:
        text = call.get("resp_body_text")
        if not call.get("action_id") or not isinstance(text, str) or len(text) % 2:
            kept.append(call)
            continue
        try:
            json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            kept.append(call)
            continue
        half = text[: len(text) // 2]
        if (
            half
            and text[len(text) // 2 :] == half
            and any(
                request_matches(call, canonical)
                for canonical in bodies.get(key(call), {}).get(half, [])
            )
        ):
            try:
                json.loads(half)
            except (json.JSONDecodeError, TypeError):
                kept.append(call)
            continue
        kept.append(call)
    return kept


# ── JSONPath 窄子集求值 ────────────────────────────────────────────────────
# 刻意只覆盖 PH 集成表实际用到的语法（点路径 / 数组下标 / [*] 通配 /
# [?(@.f=='v')] 过滤，过滤内 && 合取），不引 jsonpath 库、纯 stdlib。
# 复杂/自然语言断言（"第一件price=realPrice*2" 等跨元素关系）不在子集内，
# 由 AI 降解阶段判定 needs_manual，不硬塞求值器。取值恒返回 node-set（list）：
# 纯标量路径 → 命中 [value] / 未命中 []；[*]/filter → 多值。

def _dig_scalar(obj: Any, path: str) -> tuple[bool, Any]:
    """过滤谓词内 @.f / @.a.b 的取值：只走点路径与数组下标，返回 (found, value)。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def _match_filter(el: Any, terms: list[tuple[str, str, Any]]) -> bool:
    if not isinstance(el, dict):
        return False
    for field, op, val in terms:
        found, actual = _dig_scalar(el, field)
        eq = found and _eq(actual, val)
        if not (eq if op == "==" else (found and not _eq(actual, val))):
            return False
    return True


def _apply_seg(nodes: list[Any], seg: tuple[str, Any]) -> list[Any]:
    typ, arg = seg
    out: list[Any] = []
    for nd in nodes:
        if typ == "key":
            if isinstance(nd, dict) and arg in nd:
                out.append(nd[arg])
            elif isinstance(nd, list) and str(arg).lstrip("-").isdigit() and -len(nd) <= int(arg) < len(nd):
                out.append(nd[int(arg)])  # 兼容旧点路径数组下标 a.0.b
        elif typ == "index":
            if isinstance(nd, list) and -len(nd) <= arg < len(nd):
                out.append(nd[arg])
        elif typ == "wild":
            if isinstance(nd, list):
                out.extend(nd)
            elif isinstance(nd, dict):
                out.extend(nd.values())
        elif typ == "filter":
            candidates = nd if isinstance(nd, list) else [nd]
            out.extend(el for el in candidates if _match_filter(el, arg))
    return out


def _jsonpath(obj: Any, path: str) -> list[Any]:
    """在 obj 上求 path，返回命中 node-set（可空）。纯标量路径命中即 [value]。"""
    nodes: list[Any] = [obj]
    for seg in _parse_path(path):
        nodes = _apply_seg(nodes, seg)
    return nodes


def static_validate_expect_entries(entries: Any, *, kind: str = "expect") -> None:
    """静态闸：批量校验 expect/forbid 条目（match 非空、assert path 可解析、op 在白名单）。

    降解器落库前用；任一不过抛 ValueError（fail-closed，拒绝写脏断言）。
    """
    if not isinstance(entries, list):
        raise ValueError(f"{kind} 须为列表")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{kind}[{i}] 须为对象")
        match = entry.get("match")
        if not isinstance(match, dict) or not (match.get("url_glob") or match.get("method")):
            raise ValueError(f"{kind}[{i}].match 须含 url_glob 或 method")
        bind = entry.get("bind")
        if bind is not None and bind not in {"first", "last"}:
            raise ValueError(f"{kind}[{i}].bind={bind!r} 非法：须为 first/last")
        for key, spec in (entry.get("assert") or {}).items():
            static_check_assert_key(str(key))
            op = _spec_op(spec)
            if op not in ALLOWED_OPS:
                raise ValueError(f"{kind}[{i}].assert[{key}] 的 op={op!r} 不在白名单 {sorted(ALLOWED_OPS)}")
            if op in CROSS_ELEMENT_OPS:
                _static_check_cross_op(f"{kind}[{i}].assert[{key}]", op, spec)


def _header_lookup(headers: Any, name: str) -> list[Any]:
    if not isinstance(headers, Mapping):
        return []
    target = name.lower()
    return [value for key, value in headers.items() if str(key).lower() == target]


def _query_lookup(call: Mapping[str, Any], param: str) -> list[Any]:
    url = call.get("url") or ""
    if isinstance(url, str) and url:
        parsed = parse_qs(urlsplit(url).query, keep_blank_values=True)
        if param in parsed:
            return list(parsed[param])
    req_query = call.get("req_query")
    if isinstance(req_query, Mapping) and param in req_query:
        val = req_query[param]
        return list(val) if isinstance(val, list) else [val]
    return []


def _resolve_nodes(call: dict, key: str) -> list[Any]:
    """断言键 → node-set。支持：status / resp_error / req.query.<p> / req.header.<h> / resp.header.<h> / resp.<path> / req.<path>。"""
    if key == "status":
        s = call.get("status")
        try:
            return [int(s)]
        except (TypeError, ValueError):
            return [] if s is None else [s]
    if key == "resp_error":
        return [call.get("resp_error")]
    if key.startswith("req.query."):
        return _query_lookup(call, key[len("req.query."):])
    if key.startswith("req.header."):
        return _header_lookup(call.get("req_headers"), key[len("req.header."):])
    if key.startswith("resp.header."):
        return _header_lookup(call.get("resp_headers"), key[len("resp.header."):])
    for side in ("resp", "req"):
        prefix = f"{side}."
        if key.startswith(prefix):
            body = _body_json(call, side)
            if body is None:
                return []
            sub = key[len(prefix):]
            if sub.startswith("body."):  # 兼容 resp.body.x 写法
                sub = sub[len("body."):]
            return _jsonpath(body, sub)
    return []


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _eq(a: Any, b: Any) -> bool:
    """相等：先原样比，再数值等价（5 == '5' == 5.0）。单位换算是 fixture 责任，非此层。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if a == b:
        return True
    na, nb = _num(a), _num(b)
    return na is not None and nb is not None and na == nb


def _nonempty(v: Any) -> bool:
    return v not in (None, "", [], {})


def _contains(node: Any, expected: Any) -> bool:
    if isinstance(node, str):
        return str(expected) in node
    if isinstance(node, (list, tuple, dict)):
        return expected in node
    return False


# op 语义都作用在 node-set 上：eq=全等且非空、empty=空或全空、exists=有非空节点、
# contains=任一节点含、present/regex=沿用旧易变字段口径（§九）。
def _eval_op(op: str, nodes: list[Any], expected: Any) -> tuple[bool, Any]:
    got = nodes[0] if len(nodes) == 1 else nodes
    if op == "exists":
        return (any(_nonempty(n) for n in nodes)), got
    if op == "empty":
        return (all(not _nonempty(n) for n in nodes)), got
    if op == "present":
        return (len(nodes) > 0 and any(_nonempty(n) for n in nodes)), got
    if op == "regex":
        return (any(n is not None and re.search(str(expected), str(n)) for n in nodes)), got
    if op == "contains":
        return (any(_contains(n, expected) for n in nodes)), got
    if op in ("eq", "=="):
        return (len(nodes) > 0 and all(_eq(n, expected) for n in nodes)), got
    if op in ("ne", "!="):
        return (len(nodes) > 0 and all(not _eq(n, expected) for n in nodes)), got
    raise ValueError(f"unknown op: {op!r}（支持 eq/ne/empty/exists/contains/present/regex）")


def _norm_spec(spec: Any, ctx: dict) -> tuple[str, Any]:
    """assert 值 → (op, expected)。对象形 {op,value} 显式；标量/魔法串走旧口径。"""
    if isinstance(spec, dict):
        return str(spec.get("op", "eq")), _subst(spec.get("value"), ctx)
    expected = _subst(spec, ctx)
    if expected == "*present*":
        return "present", None
    if isinstance(expected, str) and expected.startswith("*regex:"):
        return "regex", expected[len("*regex:"):].rstrip("*")
    return "eq", expected


def _match_call(call: dict, match: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    if "method" in match and (call.get("method") or "").upper() != str(_subst(match["method"], ctx)).upper():
        return False
    url = call.get("url") or ""
    if "url_glob" in match:
        if not fnmatch.fnmatch(url, str(_subst(match["url_glob"], ctx))):
            return False
    if "url_regex" in match:
        if re.search(str(_subst(match["url_regex"], ctx)), url) is None:
            return False
    if "path" in match:
        url_path = urlsplit(url).path
        expected_path = str(_subst(match["path"], ctx))
        if any(ch in expected_path for ch in "*?["):
            if not fnmatch.fnmatch(url_path, expected_path):
                return False
        elif url_path != expected_path:
            return False
    return True


def _is_post_body_housekeeping(call: Mapping[str, Any]) -> bool:
    return (
        str(call.get("step") or "") in POST_BODY_HOUSEKEEPING_STEPS
        or str(call.get("window_tag") or "") in POST_BODY_HOUSEKEEPING_STEPS
    )


def _scope_call(
    call: dict,
    step: str | None,
    *,
    window: Mapping[str, Any] | None = None,
) -> bool:
    explicit_housekeeping = (
        step in POST_BODY_HOUSEKEEPING_STEPS
        or (
            isinstance(window, Mapping)
            and (
                window.get("step") in POST_BODY_HOUSEKEEPING_STEPS
                or window.get("window_tag") in POST_BODY_HOUSEKEEPING_STEPS
            )
        )
    )
    if not explicit_housekeeping and _is_post_body_housekeeping(call):
        return False
    if step is not None:
        # New live captures carry operation=<typed-IR operation name>; keep legacy
        # action-step expectations working by also accepting the action-level step.
        if not (call.get("operation") == step or call.get("step") == step):
            return False
    if isinstance(window, Mapping):
        for field in ("symbol_id", "operation", "step", "window_tag", "action_id"):
            expected = window.get(field)
            if expected is not None and call.get(field) != expected:
                return False
    return True


def _select_hit(hit: list[dict], bind: str | None) -> tuple[dict | None, dict | None]:
    if not hit:
        return None, None
    if bind == "first":
        return hit[0], None
    if bind == "last":
        return hit[-1], None
    if len(hit) > 1:
        return None, {
            "verdict": "fail",
            "reason": "ambiguous",
            "detail": f"命中 {len(hit)} 条调用但未声明 bind:first|last，拒绝静默取首条",
        }
    return hit[0], None


# ── 跨元素关系断言原语（needs_manual 收编，§六追加） ────────────────────────
# 三类模式实证于 285 行集成用例批转化：①第二份半价类校验②赠品关联③计算型金额。
# 都需要在同一 call 内比较多个字段/元素，超出「单路径 vs 标量」的 _eval_op 口径，
# 所以各自专开求值函数，由 _assert_value 按 op 提前分流过去。

def _resolve_single(call: dict, path: str, ctx: dict) -> tuple[bool, Any]:
    """resolve 一个 JSONPath，要求恰好命中一个节点（0 或 >1 都算未定 found=False）。"""
    nodes = _resolve_nodes(call, _subst(path, ctx))
    if len(nodes) != 1:
        return False, None
    return True, nodes[0]


def _terminal_is_nodeset(key: str) -> bool:
    """path 末段是 filter/wildcard（求值出一串**元素** node-set）还是 key/index（求值出单个
    **容器**节点）。用来让跨元素 op 同时吃两种形态：`resp.items`（末段 key，拿到那个 list）与
    `resp.items[?(@.f=='v')]` / `resp.items[*]`（末段 filter/wild，node-set 本身即元素数组）。"""
    if key in ("status", "resp_error") or key.startswith(("req.query.", "req.header.", "resp.header.")):
        return False
    for side in ("resp", "req"):
        if key.startswith(f"{side}."):
            sub = key[len(side) + 1:]
            if sub.startswith("body."):
                sub = sub[len("body."):]
            segs = _parse_path(sub)
            return bool(segs) and segs[-1][0] in ("filter", "wild")
    return False


def _resolve_array(call: dict, path: str, ctx: dict) -> tuple[bool, list[Any] | None]:
    """把跨元素 op 的 path 解析成「元素数组」，接受两种等价形态（prompt 明说
    half_price_pair/gift_relation 的 path 可指向 `items` **或过滤出的子数组**）：
      · base 数组路径（末段 key/index，如 `resp.items`）→ 命中的那个单一 list 节点；
      · node-set 路径（末段 filter/wild，如 `resp.items[?(@.promotionType=='2')]`）→ 命中的
        元素 node-set 整体当数组。
    命中不出数组（base 路径命中非 list / 命中数≠1，或 node-set 路径空）→ (False, None)。"""
    nodes = _resolve_nodes(call, _subst(path, ctx))
    if _terminal_is_nodeset(_subst(path, ctx)):
        return (True, nodes) if nodes else (False, None)
    if len(nodes) == 1 and isinstance(nodes[0], list):
        return True, nodes[0]
    return False, None


def _eval_half_price_pair(call: dict, key: str, spec: dict, ctx: dict) -> dict:
    """第 i 个元素 price_field == base_field × factors[i]（第二份半价一类：按位对齐的倍率校验）。"""
    price_field, base_field, factors = spec["price_field"], spec["base_field"], spec["factors"]
    found, arr = _resolve_array(call, key, ctx)
    want = f"每元素 {price_field} == {base_field}×factors{factors}（按位对齐）"
    if not found or arr is None:
        return {"key": key, "op": "half_price_pair", "ok": False, "want": want, "got": "<missing/not-list>"}
    bad: list[str] = []
    for i, factor in enumerate(factors):
        if i >= len(arr) or not isinstance(arr[i], dict):
            bad.append(f"[{i}] 缺元素")
            continue
        item = arr[i]
        price, base = _num(item.get(price_field)), _num(item.get(base_field))
        if price is None or base is None or not _eq(price, base * factor):
            bad.append(f"[{i}] {price_field}={item.get(price_field)!r} 应={base_field}×{factor}"
                       + (f"={base * factor}" if base is not None else ""))
    return {"key": key, "op": "half_price_pair", "ok": not bad, "want": want,
            "got": "ok" if not bad else "; ".join(bad)}


def _eval_gift_relation(call: dict, key: str, spec: dict, ctx: dict) -> dict:
    """赠品元素（gift_filter 命中）的 link_field 应等于某个其余元素的 target_field（赠品关联）。"""
    gift_filter, link_field, target_field = spec["gift_filter"], spec["link_field"], spec["target_field"]
    found, arr = _resolve_array(call, key, ctx)
    want = f"gift_filter{gift_filter} 命中元素的 {link_field} == 某元素的 {target_field}"
    if not found or arr is None:
        return {"key": key, "op": "gift_relation", "ok": False, "want": want, "got": "<missing/not-list>"}
    gifts = [n for n in arr if isinstance(n, dict)
             and all(_eq(n.get(f), v) for f, v in gift_filter.items())]
    if not gifts:
        return {"key": key, "op": "gift_relation", "ok": False, "want": want,
                "got": "未找到匹配 gift_filter 的元素"}
    targets = [n for n in arr if isinstance(n, dict) and n not in gifts]
    target_ids = {n.get(target_field) for n in targets}
    bad = [g for g in gifts if not any(_eq(g.get(link_field), t) for t in target_ids)]
    return {"key": key, "op": "gift_relation", "ok": not bad, "want": want,
            "got": "ok" if not bad else
            "; ".join(f"gift.{link_field}={g.get(link_field)!r} 不在其余元素 {target_field} 集合 {target_ids!r}"
                      for g in bad)}


def _eval_computed_amount(call: dict, key: str, spec: dict, ctx: dict) -> dict:
    """key 处的标量应等于 formula(refs) 的算术结果（计算型金额：原价-展示价 / 原价×折扣率等）。"""
    refs, formula = spec["refs"], spec["formula"]
    want = f"{key} == {formula}（refs={refs}）"
    found, actual = _resolve_single(call, key, ctx)
    actual_num = _num(actual) if found else None
    if actual_num is None:
        return {"key": key, "op": "computed_amount", "ok": False, "want": want, "got": "<missing/non-numeric>"}
    values: dict[str, float] = {}
    for name, path in refs.items():
        rfound, rval = _resolve_single(call, path, ctx)
        rnum = _num(rval) if rfound else None
        if rnum is None:
            return {"key": key, "op": "computed_amount", "ok": False, "want": want,
                    "got": f"ref {name}({path}) 缺失或非数值"}
        values[name] = rnum
    expected = _eval_formula(formula, values)
    ok = _eq(actual_num, expected)
    return {"key": key, "op": "computed_amount", "ok": ok, "want": want,
            "got": f"{actual_num!r}（公式算得 {expected!r}）"}


def _numeric_refs(call: dict, refs: dict, ctx: dict) -> tuple[dict[str, float] | None, str | None]:
    values: dict[str, float] = {}
    for name, path in refs.items():
        found, value = _resolve_single(call, path, ctx)
        number = _num(value) if found else None
        if number is None:
            return None, f"ref {name}({path}) 缺失或非数值"
        values[str(name)] = number
    return values, None


def _eval_aggregate_formula(call: dict, key: str, spec: dict, ctx: dict) -> dict:
    """数组字段求和后等于同一调用内的算式（优惠分摊总额一类）。"""
    nodes = _resolve_nodes(call, _subst(key, ctx))
    numbers = [_num(node) for node in nodes]
    want = f"sum({key}) == {spec['formula']}（refs={spec['refs']}）"
    if not numbers or any(n is None for n in numbers):
        return {"key": key, "op": "aggregate_formula", "ok": False, "want": want,
                "got": "<missing/non-numeric aggregate nodes>"}
    values, error = _numeric_refs(call, spec["refs"], ctx)
    if error:
        return {"key": key, "op": "aggregate_formula", "ok": False, "want": want, "got": error}
    assert values is not None
    expected = _eval_formula(str(spec["formula"]), values)
    actual = sum(n for n in numbers if n is not None)
    return {"key": key, "op": "aggregate_formula", "ok": _eq(actual, expected), "want": want,
            "got": f"sum={actual!r}（公式算得 {expected!r}）"}


def _eval_numeric_relation(call: dict, key: str, spec: dict, ctx: dict) -> dict:
    """比较字段大小或验证字段等于若干候选金额中的最小值。"""
    relation = str(spec["relation"])
    found, value = _resolve_single(call, key, ctx)
    actual = _num(value) if found else None
    values, error = _numeric_refs(call, spec["refs"], ctx)
    want = f"{key} {relation} refs={spec['refs']}"
    if actual is None:
        return {"key": key, "op": "numeric_relation", "ok": False, "want": want,
                "got": "<missing/non-numeric actual>"}
    if error:
        return {"key": key, "op": "numeric_relation", "ok": False, "want": want, "got": error}
    assert values is not None
    if relation == "eq_min":
        target = min(values.values())
        ok = _eq(actual, target)
        detail = f"actual={actual!r}, min={target!r}"
    else:
        target = next(iter(values.values()))
        ok = {
            "gt": actual > target,
            "ge": actual >= target,
            "lt": actual < target,
            "le": actual <= target,
        }[relation]
        detail = f"actual={actual!r}, rhs={target!r}"
    return {"key": key, "op": "numeric_relation", "ok": ok, "want": want, "got": detail}


def _eval_text_amount_formula(call: dict, key: str, spec: dict, ctx: dict) -> dict:
    """从「还差 ¥X」类文本取命名金额，再与同调用字段做单位明确的算式。"""
    nodes = _resolve_nodes(call, _subst(key, ctx))
    capture = str(spec["capture"])
    pattern = re.compile(str(_subst(spec["pattern"], ctx)))
    match = next((pattern.search(str(node)) for node in nodes if pattern.search(str(node))), None)
    want = f"{spec['formula']} == {spec['expected']!r}（text {key}, capture={capture}）"
    if match is None:
        return {"key": key, "op": "text_amount_formula", "ok": False, "want": want,
                "got": "<text pattern not matched>"}
    captured = _num(match.group(capture))
    scale = _num(_subst(spec.get("scale", 1), ctx))
    if captured is None or scale is None:
        return {"key": key, "op": "text_amount_formula", "ok": False, "want": want,
                "got": "<captured amount or scale is non-numeric>"}
    values, error = _numeric_refs(call, spec["refs"], ctx)
    if error:
        return {"key": key, "op": "text_amount_formula", "ok": False, "want": want, "got": error}
    assert values is not None
    values[capture] = captured * scale
    actual = _eval_formula(str(spec["formula"]), values)
    expected = _num(_subst(spec["expected"], ctx))
    ok = expected is not None and _eq(actual, expected)
    return {"key": key, "op": "text_amount_formula", "ok": ok, "want": want,
            "got": f"captured={captured!r}×{scale!r}; formula={actual!r}"}


_CROSS_ELEMENT_EVAL = {
    "half_price_pair": _eval_half_price_pair,
    "gift_relation": _eval_gift_relation,
    "computed_amount": _eval_computed_amount,
    "aggregate_formula": _eval_aggregate_formula,
    "numeric_relation": _eval_numeric_relation,
    "text_amount_formula": _eval_text_amount_formula,
}


def _assert_value(call: dict, key: str, spec: Any, ctx: dict) -> dict:
    """单条字段断言。key 支持 JSONPath 子集；spec 为标量/魔法串（旧）、{op,value}（新）或跨元素 op。"""
    cross_op = _spec_op(spec)
    if cross_op in CROSS_ELEMENT_OPS:
        return _CROSS_ELEMENT_EVAL[cross_op](call, key, spec, ctx)
    op, expected = _norm_spec(spec, ctx)
    nodes = _resolve_nodes(call, _subst(key, ctx))
    ok, got = _eval_op(op, nodes, expected)
    want = "*present*" if op == "present" else (
        f"*regex:{expected}*" if op == "regex" else f"{op} {expected!r}")
    return {"key": key, "op": op, "ok": ok, "want": want,
            "got": got if nodes else "<missing>"}


def evaluate_case(case_id: str, calls: list[dict], expectations: dict,
                  ctx: dict | None = None) -> list[dict]:
    """对一个 case 评估，返回 assertion 结果列表（每条含 verdict/detail）。"""
    ctx = ctx or {}
    spec = expectations.get(case_id)
    results: list[dict] = []
    if not spec:
        return results
    # 该 case 归因到的调用（step 维度可选）；噪声（deny glob 命中）不参与断言（§六）
    case_calls = _without_same_action_double_body_shadows([
        c for c in calls if c.get("case_id") == case_id and not c.get("noise")
    ])

    for exp in spec.get("expect", []) or []:
        match = exp.get("match", {})
        step = exp.get("step")
        window = exp.get("window") if isinstance(exp.get("window"), Mapping) else None
        bind = exp.get("bind")
        missing_vars = _unresolved_vars(exp, ctx)
        if missing_vars:
            results.append({"kind": "expect", "match": match, "step": step,
                            "verdict": "fail", "reason": "unresolved_var",
                            "detail": "fixture 上下文缺变量: "
                                      + ", ".join(missing_vars)
                                      + "（检查 AP_FIXTURE_CONTEXT / --fixture-context）"})
            continue
        effective_window = (
            {k: _subst(v, ctx) for k, v in window.items()} if window is not None else None
        )
        pool = [c for c in case_calls if _scope_call(c, step, window=effective_window)]
        hit = [c for c in pool if _match_call(c, match, ctx)]
        if not hit:
            results.append({"kind": "expect", "match": match, "step": step,
                            "verdict": "fail", "reason": "missing",
                            "detail": "期望的调用未发生"})
            continue
        call, select_error = _select_hit(hit, bind)
        if select_error:
            results.append({"kind": "expect", "match": match, "step": step, "bind": bind,
                            **select_error})
            continue
        assert call is not None
        field_results = [_assert_value(call, k, v, ctx)
                         for k, v in (exp.get("assert") or {}).items()]
        bad = [f for f in field_results if not f["ok"]]
        results.append({
            "kind": "expect", "match": match, "step": step, "bind": bind,
            "verdict": "fail" if bad else "pass",
            "reason": "assert" if bad else "ok",
            "url": call.get("url"), "status": call.get("status"),
            "fields": field_results,
            "detail": "; ".join(f"{f['key']} want={f['want']!r} got={f['got']!r}" for f in bad) or "ok",
        })

    for fb in spec.get("forbid", []) or []:
        match = fb.get("match", {})
        step = fb.get("step")
        window = fb.get("window") if isinstance(fb.get("window"), Mapping) else None
        missing_vars = _unresolved_vars(fb, ctx)
        if missing_vars:
            # forbid 的 glob 永不匹配 == 静默放行，比 expect 更危险，同样 fail-closed
            results.append({"kind": "forbid", "match": match, "step": step,
                            "verdict": "fail", "reason": "unresolved_var",
                            "detail": "fixture 上下文缺变量: "
                                      + ", ".join(missing_vars)
                                      + "（检查 AP_FIXTURE_CONTEXT / --fixture-context）"})
            continue
        effective_window = (
            {k: _subst(v, ctx) for k, v in window.items()} if window is not None else None
        )
        pool = [c for c in case_calls if _scope_call(c, step, window=effective_window)]
        hit = [c for c in pool if _match_call(c, match, ctx)]
        if hit:
            results.append({"kind": "forbid", "match": match, "step": step,
                            "verdict": "fail", "reason": "unexpected",
                            "url": hit[0].get("url"),
                            "detail": "不该出现的调用出现了"})
    return results


def _references_entity_var(obj: Any) -> bool:
    if isinstance(obj, str):
        return any(name.startswith("entity.") for name in _VAR_RE.findall(obj))
    if isinstance(obj, Mapping):
        return any(_references_entity_var(k) or _references_entity_var(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_references_entity_var(item) for item in obj)
    return False


def _merge_window(
    default_window: Mapping[str, Any] | None,
    rule_window: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    if isinstance(default_window, Mapping):
        merged.update(default_window)
    if isinstance(rule_window, Mapping):
        merged.update(rule_window)
    return merged or None


def _rule_match_dict(rule: Mapping[str, Any]) -> dict[str, Any]:
    match: dict[str, Any] = {}
    nested = rule.get("match")
    if isinstance(nested, Mapping):
        match.update(nested)
    for key in ("method", "path", "url_glob", "url_regex"):
        if key in rule:
            match[key] = rule[key]
    return match


def evaluate_entity_api_assertions(
    case_id: str,
    calls: list[dict[str, Any]],
    api_window: Mapping[str, Any],
    ctx: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate case-level ``entity_binding.api_window`` expectations & prohibitions.

    Reuses the oracle's JSONPath, query/header/resp_error, and cross-element assertion
    engine with ``${entity.id}`` / ``${entity.<attr>}`` / ``${<var>}`` substitution,
    strictly scoping calls to the declared interaction window while excluding
    post-body housekeeping marks (``body_end``, ``post_case_cleanup``, ``entity_readback``).
    """
    if not isinstance(api_window, Mapping):
        return []
    ctx_dict = dict(ctx or {})
    default_window = (
        api_window.get("window")
        if isinstance(api_window.get("window"), Mapping)
        else None
    )
    case_calls = _without_same_action_double_body_shadows([
        c for c in calls
        if c.get("case_id") == case_id
        and not c.get("noise")
        and not _is_post_body_housekeeping(c)
    ])

    results: list[dict[str, Any]] = []

    for exp in api_window.get("expect") or []:
        if not isinstance(exp, Mapping):
            continue
        name = exp.get("name")
        match = _rule_match_dict(exp)
        rule_window = _merge_window(
            default_window,
            exp.get("window") if isinstance(exp.get("window"), Mapping) else None,
        )
        bind = exp.get("bind")
        assertions_spec = exp.get("assert") if isinstance(exp.get("assert"), Mapping) else {}
        missing_vars = _unresolved_vars(
            {"match": match, "window": rule_window or {}, "assert": assertions_spec},
            ctx_dict,
        )
        if missing_vars:
            results.append({
                "kind": "expect",
                "source": "entity_binding",
                **({"name": str(name)} if name else {}),
                "match": match,
                "window": rule_window,
                "verdict": "fail",
                "reason": "unresolved_var",
                "missing_vars": missing_vars,
                "detail": "entity_binding 上下文缺变量: " + ", ".join(missing_vars),
            })
            continue
        effective_window = (
            {k: _subst(v, ctx_dict) for k, v in rule_window.items()}
            if rule_window is not None
            else None
        )
        pool = [c for c in case_calls if _scope_call(c, None, window=effective_window)]
        hit = [c for c in pool if _match_call(c, match, ctx_dict)]
        if not hit:
            results.append({
                "kind": "expect",
                "source": "entity_binding",
                **({"name": str(name)} if name else {}),
                "match": match,
                "window": effective_window,
                "verdict": "fail",
                "reason": "missing",
                "detail": "期望的交互窗口调用未发生",
            })
            continue
        call, select_error = _select_hit(hit, bind)
        if select_error:
            results.append({
                "kind": "expect",
                "source": "entity_binding",
                **({"name": str(name)} if name else {}),
                "match": match,
                "window": effective_window,
                "bind": bind,
                **select_error,
            })
            continue
        assert call is not None
        field_results: list[dict[str, Any]] = []
        for raw_key, raw_spec in assertions_spec.items():
            evaluated = _assert_value(call, str(raw_key), raw_spec, ctx_dict)
            if _references_entity_var(raw_key) or _references_entity_var(raw_spec):
                evaluated["entity_bound"] = True
            field_results.append(evaluated)
        bad = [f for f in field_results if not f["ok"]]
        results.append({
            "kind": "expect",
            "source": "entity_binding",
            **({"name": str(name)} if name else {}),
            "match": match,
            "window": effective_window,
            "bind": bind,
            "verdict": "fail" if bad else "pass",
            "reason": "assert" if bad else "ok",
            "url": call.get("url"),
            "status": call.get("status"),
            "resp_error": call.get("resp_error"),
            "fields": field_results,
            "detail": "; ".join(f"{f['key']} want={f['want']!r} got={f['got']!r}" for f in bad) or "ok",
        })

    for fb in api_window.get("forbid") or []:
        if not isinstance(fb, Mapping):
            continue
        name = fb.get("name")
        match = _rule_match_dict(fb)
        rule_window = _merge_window(
            default_window,
            fb.get("window") if isinstance(fb.get("window"), Mapping) else None,
        )
        missing_vars = _unresolved_vars(
            {"match": match, "window": rule_window or {}},
            ctx_dict,
        )
        if missing_vars:
            results.append({
                "kind": "forbid",
                "source": "entity_binding",
                **({"name": str(name)} if name else {}),
                "match": match,
                "window": rule_window,
                "verdict": "fail",
                "reason": "unresolved_var",
                "missing_vars": missing_vars,
                "detail": "entity_binding 上下文缺变量: " + ", ".join(missing_vars),
            })
            continue
        effective_window = (
            {k: _subst(v, ctx_dict) for k, v in rule_window.items()}
            if rule_window is not None
            else None
        )
        pool = [c for c in case_calls if _scope_call(c, None, window=effective_window)]
        hit = [c for c in pool if _match_call(c, match, ctx_dict)]
        if hit:
            results.append({
                "kind": "forbid",
                "source": "entity_binding",
                **({"name": str(name)} if name else {}),
                "match": match,
                "window": effective_window,
                "verdict": "fail",
                "reason": "unexpected",
                "url": hit[0].get("url"),
                "status": hit[0].get("status"),
                "detail": "交互窗口内出现了禁止的调用",
            })
        else:
            results.append({
                "kind": "forbid",
                "source": "entity_binding",
                **({"name": str(name)} if name else {}),
                "match": match,
                "window": effective_window,
                "verdict": "pass",
                "reason": "ok",
                "detail": "ok",
            })

    return results


def api_status(assertions: list[dict]) -> str | None:
    """API 断言合成态：任一 fail → fail；有断言全 pass → pass；无断言 → None（不参与合成）。"""
    if not assertions:
        return None
    return "fail" if any(a["verdict"] == "fail" for a in assertions) else "pass"


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", type=Path)
    ap.add_argument("case_id")
    ap.add_argument("--exp", type=Path, default=DEFAULT_EXPECTATIONS)
    ap.add_argument("--statuses", default="active",
                    help="list 形断言资产按 status 过滤（逗号分隔；'all' 不过滤）")
    ap.add_argument("--fixture-context", type=Path, default=None,
                    help="Phase 5 fixture_context.json for ${var} substitutions")
    ap.add_argument("--fixtures", type=Path, default=None,
                    help="扁平真值表 {CASE_ID:{var:value}}（营销/点餐外部真值，§四）")
    args = ap.parse_args(argv)

    calls = _read_jsonl(args.jsonl)
    statuses = None if args.statuses.strip().lower() == "all" else tuple(
        s.strip() for s in args.statuses.split(",") if s.strip())
    exps = load_expectations(args.exp, statuses=statuses)
    ctx = {**fixture_context_vars(args.fixture_context),
           **flat_fixture_vars(args.fixtures, args.case_id)}
    results = evaluate_case(args.case_id, calls, exps, ctx=ctx)
    print(f"== api_oracle: {args.case_id} ({len(calls)} calls in jsonl) ==")
    if ctx:
        print(f"  fixture vars: {', '.join(sorted(ctx))}")
    if not exps.get(args.case_id):
        print(f"  （{args.case_id} 无 expectations）")
        return 0
    for r in results:
        print(f"  [{r['verdict']}/{r['reason']}] {r['kind']} {r.get('match')} "
              f"{r.get('status') or ''} → {r['detail']}")
    print(f"  合成 API 态：{api_status(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
