"""接口场景规则：静态校验 + host 侧编译 + 快照 hash + 环境闸（纯函数、无 frida、可测）。

真源是「场景模板」（版本化资源，形如 docs §七 的规则模型）：

    {
      "id": "sc-order-empty",
      "app": "food_ordering",
      "env": "uat",
      "rules": [
        {
          "rule_id": "r1",
          "name": "订单列表返回空",
          "match": {"method": "POST", "host": "api.example.com",
                    "path": "/order/list", "query": {"page": "1"}},
          "action": {"type": "fixed_json", "status": 200, "headers": {}, "body": "{\"code\":0,\"data\":[]}"},
          "times": {"mode": "first_n", "n": 1},
          "assert_hit": true
        }
      ]
    }

编译产物（``configureScenario`` 下发给 agent 的 ``rules``）是**去掉展示字段的最小执行形**；
``rule_snapshot_hash`` 对**编译后 rules 序列化**取 sha256（§6.1：锁的/校验的/执行的是同一串字节，
源规则没变但编译产物变了也会换 hash）。规则时序（先于 resume）、hook 装载由 controller/agent 负责，
本模块只管「规则合法 + 编译 + 指纹」。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# 第一期只做两种行为（§一 / §七）；其余（HTTP 错误 / 延迟 / 顺序响应 / 挂起超时）穿透证明后再扩。
SUPPORTED_ACTIONS = frozenset({"fixed_json", "timeout"})
SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"})
SUPPORTED_TIMES = frozenset({"always", "first_n"})
# 环境名只负责选择已注册 target，不再承载「Prod 天生不可用」的策略。
# 具体 App/host/path 是否可用由上层 target resolver 决定；规则层仅要求非空环境名。


class ScenarioError(ValueError):
    """场景校验/编译的 fail-closed 异常：任一 error 级诊断即抛，会话绝不成立。"""


@dataclass(frozen=True)
class Diagnostic:
    """一条诊断。``level='error'`` 阻断启用；``level='warn'`` 仅提示。"""

    level: str          # error | warn
    message: str
    rule_id: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level == "error"


@dataclass(frozen=True)
class CompiledScenario:
    """编译产物：agent 侧哑执行的最小形 + 快照指纹。

    ``payload(mode)`` 生成 ``configureScenario`` 的 RPC 入参（附挂载模型，仅供 agent
    health 诚实上报覆盖缺口，不进 hash）。"""

    scenario_id: str
    app: str
    env: str
    rules: list[dict[str, Any]]
    snapshot_hash: str
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def payload(self, mode: str | None = None) -> dict[str, Any]:
        return {"rules": self.rules, "hash": self.snapshot_hash, "mode": mode}


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None


def validate(scenario: dict[str, Any]) -> list[Diagnostic]:
    """静态校验规则集，返回诊断列表（error 阻断、warn 提示）。不抛异常。

    覆盖：结构必填、行为白名单（第一期只 fixed_json/timeout）、fixed_json 的 body 必须是合法
    JSON（第一期只固定 JSON）、method/times 白名单、rule_id 唯一、query/header 标量、
    以及**明确排除项的诚实拒绝**（body 字段匹配、延迟/顺序/HTTP错误等 → error 指向 §一/§七）。"""
    diags: list[Diagnostic] = []
    if not isinstance(scenario, dict):
        return [Diagnostic("error", "scenario 必须是对象")]

    if not str(scenario.get("id") or "").strip():
        diags.append(Diagnostic("error", "scenario.id 必填"))
    if not str(scenario.get("app") or "").strip():
        diags.append(Diagnostic("error", "scenario.app 必填"))

    env = str(scenario.get("env") or "").strip().lower()
    if not env:
        diags.append(Diagnostic("error", "scenario.env 必填"))

    rules = scenario.get("rules")
    if not isinstance(rules, list) or not rules:
        diags.append(Diagnostic("error", "scenario.rules 至少一条"))
        return diags

    seen_ids: set[str] = set()
    for i, raw in enumerate(rules):
        rid = str((_as_dict(raw).get("rule_id")) or "").strip() or f"#{i}"
        diags.extend(_validate_rule(raw, rid, seen_ids))
    return diags


def _validate_rule(raw: Any, rid: str, seen_ids: set[str]) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    rule = _as_dict(raw)
    if not rule:
        return [Diagnostic("error", "规则必须是对象", rid)]

    if not str(rule.get("rule_id") or "").strip():
        diags.append(Diagnostic("error", "rule_id 必填", rid))
    elif rid in seen_ids:
        diags.append(Diagnostic("error", f"rule_id 重复: {rid}", rid))
    else:
        seen_ids.add(rid)

    match = _as_dict(rule.get("match"))
    method = str(match.get("method") or "").strip().upper()
    if not method:
        diags.append(Diagnostic("error", "match.method 必填", rid))
    elif method not in SUPPORTED_METHODS:
        diags.append(Diagnostic("error", f"match.method={method!r} 非法", rid))
    if not str(match.get("host") or "").strip():
        diags.append(Diagnostic("error", "match.host 必填", rid))
    path = str(match.get("path") or "").strip()
    path_prefix = str(match.get("path_prefix") or "").strip()
    path_suffix = str(match.get("path_suffix") or "").strip()
    if path and (path_prefix or path_suffix):
        diags.append(Diagnostic("error", "match.path 与 path_prefix/path_suffix 互斥", rid))
    elif not path and not path_prefix:
        diags.append(Diagnostic(
            "error", "match.path 或 match.path_prefix 必填（受限路径匹配）", rid
        ))
    elif path and not path.startswith("/"):
        diags.append(Diagnostic("error", "match.path 必须以 / 开头", rid))
    elif path_prefix and not path_prefix.startswith("/"):
        diags.append(Diagnostic("error", "match.path_prefix 必须以 / 开头", rid))
    if path_suffix and not path_prefix:
        diags.append(Diagnostic("error", "match.path_suffix 只能与 path_prefix 同用", rid))
    exact_url = match.get("exact_url")
    if "exact_url" in match:
        try:
            parsed = urlsplit(exact_url) if isinstance(exact_url, str) else None
            valid = (parsed is not None and parsed.scheme in ("http", "https")
                     and parsed.hostname == match.get("host") and (parsed.path or "/") == path
                     and not parsed.fragment and not parsed.username and not parsed.password)
        except ValueError:
            valid = False
        if not valid:
            diags.append(Diagnostic("error", "match.exact_url 非法或与 host/path 不符", rid))
    query = match.get("query")
    if query is not None:
        if not isinstance(query, dict):
            diags.append(Diagnostic("error", "match.query 必须是对象", rid))
        elif not all(_scalar(v) for v in query.values()):
            diags.append(Diagnostic("error", "match.query 值必须是标量", rid))
    # 第一期匹配不依赖 body（upload/stream body 在 fromData 读不到，§6.2 / §十二）。
    if "body" in match or "body_contains" in match or "body_fields" in match:
        diags.append(Diagnostic(
            "error", "第一期匹配不支持 body 字段条件（仅 method+域名+路径+query，§6.2）", rid))

    action = _as_dict(rule.get("action"))
    atype = str(action.get("type") or "").strip()
    if not atype:
        diags.append(Diagnostic("error", "action.type 必填", rid))
    elif atype not in SUPPORTED_ACTIONS:
        diags.append(Diagnostic(
            "error", f"action.type={atype!r} 第一期不支持（仅 fixed_json / timeout；"
            "HTTP错误/延迟/顺序响应/挂起超时见 §一/§七，穿透证明后再扩）", rid))
    elif atype == "fixed_json":
        diags.extend(_validate_fixed_json(action, rid))

    times = _as_dict(rule.get("times")) or {"mode": "always"}
    tmode = str(times.get("mode") or "always").strip()
    if tmode not in SUPPORTED_TIMES:
        diags.append(Diagnostic("error", f"times.mode={tmode!r} 非法（always | first_n）", rid))
    elif tmode == "first_n":
        n = times.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            diags.append(Diagnostic("error", "times.mode=first_n 需要正整数 n", rid))
    return diags


def _validate_fixed_json(action: dict[str, Any], rid: str) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    status = action.get("status", 200)
    if not isinstance(status, int) or isinstance(status, bool) or not (100 <= status <= 599):
        diags.append(Diagnostic("error", "action.status 需在 100..599", rid))
    headers = action.get("headers")
    if headers is not None:
        if not isinstance(headers, dict) or not all(_scalar(v) for v in headers.values()):
            diags.append(Diagnostic("error", "action.headers 必须是标量值对象", rid))
    body = action.get("body")
    if body is None or (isinstance(body, str) and not body.strip()):
        diags.append(Diagnostic("error", "action.type=fixed_json 需要非空 body（第一期只固定 JSON）", rid))
    else:
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        try:
            json.loads(text)
        except (ValueError, TypeError):
            diags.append(Diagnostic("error", "action.body 不是合法 JSON（第一期只固定 JSON，§一）", rid))
    return diags


def errors(diags: list[Diagnostic]) -> list[Diagnostic]:
    return [d for d in diags if d.is_error]


def compile_rules(scenario: dict[str, Any]) -> CompiledScenario:
    """校验 → 编译成 agent 哑执行的最小形 → 算快照 hash。

    fail-closed：有任一 error 级诊断即抛 ``ScenarioError``（会话绝不成立，§八 闸2）。"""
    diags = validate(scenario)
    errs = errors(diags)
    if errs:
        raise ScenarioError("场景规则非法：" + "; ".join(
            (f"[{d.rule_id}] " if d.rule_id else "") + d.message for d in errs
        ))

    compiled_rules = [_compile_rule(_as_dict(r)) for r in scenario["rules"]]
    return CompiledScenario(
        scenario_id=str(scenario["id"]),
        app=str(scenario["app"]),
        env=str(scenario.get("env") or "").strip().lower(),
        rules=compiled_rules,
        snapshot_hash=rule_snapshot_hash(compiled_rules),
        diagnostics=list(diags),
    )


def _compile_rule(rule: dict[str, Any]) -> dict[str, Any]:
    match = _as_dict(rule.get("match"))
    compiled_match: dict[str, Any] = {
        "method": str(match["method"]).upper(),
        "host": str(match["host"]),
    }
    if str(match.get("path") or "").strip():
        compiled_match["path"] = str(match["path"])
    else:
        compiled_match["path_prefix"] = str(match["path_prefix"])
        if str(match.get("path_suffix") or ""):
            compiled_match["path_suffix"] = str(match["path_suffix"])
    if "exact_url" in match:
        compiled_match["exact_url"] = match["exact_url"]
    query = match.get("query")
    if isinstance(query, dict) and query:
        compiled_match["query"] = {str(k): _query_str(v) for k, v in query.items()}

    action = _as_dict(rule.get("action"))
    atype = str(action["type"])
    compiled_action: dict[str, Any] = {"type": atype}
    if atype == "fixed_json":
        body = action.get("body")
        compiled_action["status"] = int(action.get("status", 200))
        compiled_action["headers"] = {
            str(k): _query_str(v) for k, v in _as_dict(action.get("headers")).items()
        }
        compiled_action["body"] = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    times = _as_dict(rule.get("times")) or {"mode": "always"}
    tmode = str(times.get("mode") or "always")
    compiled_times: dict[str, Any] = {"mode": tmode}
    if tmode == "first_n":
        compiled_times["n"] = int(times["n"])

    return {
        "rule_id": str(rule["rule_id"]),
        "match": compiled_match,
        "action": compiled_action,
        "times": compiled_times,
        "assert_hit": bool(rule.get("assert_hit", True)),
    }


def _query_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def rule_snapshot_hash(compiled_rules: list[dict[str, Any]]) -> str:
    """对**编译后 rules 序列化**取 sha256（§6.1）。key 排序 → 与 dict 迭代序无关。"""
    blob = json.dumps(compiled_rules, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def env_gate(scenario: dict[str, Any], *, prod_hosts: "set[str] | frozenset[str]" = frozenset()) -> list[Diagnostic]:
    """环境目标闸：环境本身中立，仅挡部署侧显式列入 denylist 的 host。

    ``prod_hosts`` 保留旧参数名以兼容现有调用方，语义现为 operator-blocked hosts。正常的
    Prod/UAT/SIT/staging host 应由上层 target resolver 按 ``selected_env`` 精确注入，而非在此硬编码。
    """
    diags: list[Diagnostic] = []
    for i, raw in enumerate(scenario.get("rules") or []):
        rule = _as_dict(raw)
        rid = str(rule.get("rule_id") or f"#{i}")
        host = str(_as_dict(rule.get("match")).get("host") or "").strip().lower()
        if host and host in {h.lower() for h in prod_hosts}:
            diags.append(Diagnostic("error", f"match.host={host!r} 命中运维阻断域名，拒绝启用（§十）", rid))
    return diags


def validate_or_raise(
    scenario: dict[str, Any], *, prod_hosts: "set[str] | frozenset[str]" = frozenset()
) -> CompiledScenario:
    """校验 + 环境闸 + 编译一步到位；任一 error 抛 ``ScenarioError``。

    controller.preflight/start 的静态闸入口。返回编译产物（含 warn 诊断供 UI 展示）。"""
    env_diags = env_gate(scenario, prod_hosts=prod_hosts)
    env_errs = errors(env_diags)
    if env_errs:
        raise ScenarioError("环境闸拒绝：" + "; ".join(d.message for d in env_errs))
    compiled = compile_rules(scenario)   # 内部再跑 validate，重复 warn 无害
    # 合并环境闸的 warn（去重靠 message）
    merged = list(compiled.diagnostics)
    seen = {(d.level, d.message, d.rule_id) for d in merged}
    for d in env_diags:
        if (d.level, d.message, d.rule_id) not in seen:
            merged.append(d)
    return CompiledScenario(
        scenario_id=compiled.scenario_id,
        app=compiled.app,
        env=compiled.env,
        rules=compiled.rules,
        snapshot_hash=compiled.snapshot_hash,
        diagnostics=merged,
    )
