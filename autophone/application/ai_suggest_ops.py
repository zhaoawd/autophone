"""ai_suggest_ops.py — 导入期 AI 推荐：把用例的前置/后置文本映射到操作库的可复用操作。

用例普遍带「前置条件」和「后置动作」，它们本质就是操作库（apps/<app>/operations/）里已有的
可复用操作（造单 / 登录 / 上下岗 / 退出登录…）。本模块把一条用例的前置/后置自由文本连同该 app
**操作库白名单**喂给本地 agent，让它推荐可用操作引用（带理由），落成 case 的
`suggested_setup`/`suggested_teardown`——**候选、待人确认**，绝不直接写 `setup`/`teardown`。

与 ai_draft / ai_propose 同纪律：
1. **只产候选，人确认**：写进 `suggested_*`（待确认区），确认权在人（两栏编辑器逐条确认 →
   并入 `setup`/`teardown`）。AI 绝不自行把操作绑进会真跑的 setup/teardown。
2. **fail-closed**：agent 没回结构化 JSON → ok=False，不静默写空；每条候选落库前过
   `operations.resolve_operation`（瞎编 op / 非 active / 参数不合契约 → 丢弃该条并记原因，
   与 ai_draft 的 validate_recipe 静态闸同口径），不让坏引用污染用例。
3. **provenance 不入 case 字段**：候选只是 `{op, with, reason}`，AI 来源体现在返回 contract 的
   provider/model；用例本身不写 source:ai（与 §3.4 一致）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from autophone.assets import operations as ops_mod
from autophone.application.ai import AiRequest, CommandRunner, _subprocess_runner, invoke
from autophone.assets.apps import AppProfile, load_app_profile
from autophone.assets.cases import update_case
from autophone.execution.engine.cases_loader import load_cases

logger = logging.getLogger(__name__)

# 一条操作引用候选的形态：op 必填，with 是入参 dict，reason 是给人看的推荐理由。
# additionalProperties:false 防漂移——只认这三个键。
_REF: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op", "with", "reason"],
    "properties": {
        "op": {"type": "string", "minLength": 1, "description": "操作库里的操作 name，必须逐字来自下方清单"},
        "with": {"type": "object", "additionalProperties": False, "properties": {}, "required": [],
                 "description": "入参，键须是该操作声明过的 param 名；不确定就留空"},
        "reason": {"type": ["string", "null"], "description": "一句中文：为什么这条用例的前置/后置该用这个操作"},
    },
}

_HINTS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode", "produce", "reason"],
    "properties": {
        "mode": {
            "type": ["string", "null"],
            "enum": ["read_only", "advance", "destructive", None],
            "description": "订单前置消费语义：只读、推进状态、破坏/完成状态",
        },
        "produce": {
            "type": ["string", "null"],
            "description": "推荐 setup 置好的目标态，或正式步骤执行后的目标态",
        },
        "reason": {"type": ["string", "null"], "description": "一句中文：为什么给出该 hints"},
    },
}

# 喂模型的输出 schema：setup（前置）/ teardown（后置）两组操作引用候选，都可为空。
_SUGGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "setup": {"type": "array", "items": _REF, "description": "前置：用例跑之前要置好的状态"},
        "teardown": {"type": "array", "items": _REF, "description": "后置：用例跑完要收尾/复位的动作"},
        "hints": _HINTS,
    },
}


def _param_schema(param: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema for one operation param, kept inside Codex's strict subset."""
    ptype = str(param.get("type") or "string")
    if ptype == "enum" and param.get("values"):
        return {"type": ["string", "null"], "enum": [str(v) for v in param.get("values") or []] + [None]}
    if ptype == "int":
        return {"type": ["integer", "null"]}
    if ptype == "float":
        return {"type": ["number", "null"]}
    if ptype == "bool":
        return {"type": ["boolean", "null"]}
    return {"type": ["string", "null"]}


def _with_schema(profile: AppProfile) -> dict[str, Any]:
    """Strict schema for operation ``with`` args.

    Codex structured output rejects unconstrained objects. Build a permissive-enough
    app-local whitelist from active operation params, then keep resolve_operation as
    the authoritative per-operation validator after the model returns.
    """
    properties: dict[str, Any] = {}
    index = ops_mod.load_operations(profile)
    for op in index.values():
        if op.get("status") != "active":
            continue
        for param in op.get("params") or []:
            name = str(param.get("name") or "").strip()
            if not name or name in properties:
                continue
            properties[name] = _param_schema(param)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(properties),
        "description": "入参，键须是该操作声明过的 param 名；不确定就留空",
    }


def _suggest_schema(profile: AppProfile) -> dict[str, Any]:
    ref = {
        "type": "object",
        "additionalProperties": False,
        "required": ["op", "with", "reason"],
        "properties": {
            "op": _REF["properties"]["op"],
            "with": _with_schema(profile),
            "reason": _REF["properties"]["reason"],
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["setup", "teardown", "hints"],
        "properties": {
            "setup": {"type": "array", "items": ref, "description": "前置：用例跑之前要置好的状态"},
            "teardown": {"type": "array", "items": ref, "description": "后置：用例跑完要收尾/复位的动作"},
            "hints": _HINTS,
        },
    }

_SYSTEM = (
    "你是 autophone 的用例-操作绑定 agent。给定一条测试用例的前置条件/后置动作描述，从下方"
    "**操作库清单**里挑出能实现这些前置/后置的可复用操作，产出操作引用候选。\n"
    "  - 前置（setup）：用例正式步骤开始前必须置好的状态（如已登录、手上有单、已上岗）。\n"
    "  - 后置（teardown）：用例跑完后要做的收尾/复位（如退出登录、取消订单、复位状态）。\n"
    "**登录态前置规则**：\n"
    "  - 若用例标注「登录态前置：未登录/已登出」，必须在 setup 里加 logout 操作（确保从任何状态复位到未登录）。\n"
    "  - 若标注「登录态前置：已登录」，必须在 setup 里加 login 操作（确保进入用例前已登录）。\n"
    "  - 若标注「登录态前置：任意」，不需要登录相关操作。\n"
    "只能用清单里**逐字出现的操作 name**，不要瞎编；`with` 的键只能是该操作声明过的 param 名，"
    "拿不准就留空（让默认值生效）。每条给一句中文 reason 说明为什么这条前置/后置该用它。\n"
    "**create_order 订单复用规则**：推荐 create_order 时，默认加上订单池复用参数：\n"
    "  - `ensure: probe_first`（优先探测可复用订单，找不到再造新单）\n"
    "  - `probe_source: live`（从真机当前订单列表探测）\n"
    "  - 复用范围默认 any（复用 live 列表里 lifecycle 匹配的任意单，含手工造的测试单），**不要**显式设"
    " `reuse_scope`；只有用例明确要求「只认平台造的单」（共享环境水印闸）才 `reuse_scope: platform`。\n"
    "  - 例外：若用例明确需要新单隔离、destructive 场景、或依赖唯一订单数据，用 `ensure: always_create`。\n"
    "**lifecycle_state 目标态规则**：create_order 的 `lifecycle_state` 是造单/复用探测共用的目标订单态，"
    "按用例正式步骤的**第一个履约动作**选它要求的前置态（订单态码表）：\n"
    "  - 到店/确认到店类用例 → `lifecycle_state: accepted`（7020 已接单——确认到店按钮只在此态出现；"
    "「到店成功」是动作的结果不是前置，别选 waiting_pickup）。\n"
    "  - 取餐/确认取餐类 → `lifecycle_state: waiting_pickup`（7030 已到店待取餐——取餐按钮要求 7030 且配送员在店）。\n"
    "  - 开始送餐/送达/确认送达类 → `lifecycle_state: picked_up`（8010 已取餐）。\n"
    "  - 拿不准就不写（默认 waiting_pickup）；不要把动作完成后的状态当前置。\n"
    "**order_type 订单类型规则**：模块/用例名含「商圈」→ `order_type: district`；含「企业」→"
    " `order_type: resident_enterprise`；普通居民单是默认（resident_normal 可省略）。用例已标注订单类型时以标注为准。\n"
    "**履约定位声明规则**：到店/取餐/开始送餐/送达等履约用例，不要推荐 `place_actor_near`。\n"
    "  - 履约定位由 operation 内部的定位 guard 处理，`place_actor_near` 属于 CoreLocation spawn-gate，不能作为履约前置。\n"
    "  - 若用例描述 200 米内/外，必须把距离声明写进 `create_order.with.distance_band`：200米内=under_200m，200米外=over_200m。\n"
    "  - 距离的参照锚点随动作自动选择：到店/取餐类相对门店，送达类相对订单自带的送餐地址——都由 guard 处理，"
    "setup 里只声明 distance_band，不用（也不能）指定坐标。\n"
    "  - 取餐等要求配送员在店的操作，用例未明说距离时默认 `distance_band: under_200m`（按钮要求在店）。\n"
    "  - 若未来推荐正式履约 operation 参数，使用 `location_policy` 声明 inside/outside；setup 候选里仍不要追加定位 op。\n"
    "**订单前置 hints（advisory，不替代 setup）**：当推荐 create_order 或用例前置描述需要「有一张订单」时，"
    "同时产 `hints`：`mode` 只能是 read_only/advance/destructive；若正式步骤会推进订单状态（如取餐、送达），"
    "用 advance 并在 `produce` 写推进后的目标态（尽量用码表词：accepted/waiting_pickup/picked_up/completed）；"
    "若只是查看订单列表，用 read_only。\n"
    "**宁缺毋滥**：清单里没有能对上的操作，就把对应数组留空——不要硬凑。用例的『正式测试步骤』"
    "和『预期结果』不是前置/后置，不要为它们推荐操作（那是用例自己要做的事，不是前置状态）。"
)


def _operation_brief(profile: AppProfile) -> str:
    """把该 app 操作库里的 active 操作渲染成给模型的清单：name + summary + params 签名。

    无操作库 / 无 active 操作 → 返回空清单提示（调用方据此 fail-closed 早退）。"""
    index = ops_mod.load_operations(profile)
    lines: list[str] = []
    for name in sorted(index):
        op = index[name]
        if op.get("status") != "active":
            continue
        parts = []
        for p in op.get("params") or []:
            sig = p["name"]
            if p.get("type") == "enum" and p.get("values"):
                sig += "∈{" + "|".join(map(str, p["values"])) + "}"
            elif p.get("type"):
                sig += f":{p['type']}"
            if "default" in p:
                sig += f"={p['default']}"
            parts.append(sig)
        params = "（无参）" if not parts else "参数：" + ", ".join(parts)
        lines.append(f"  - {name}：{op.get('summary', '')}\n      {params}")
    return "\n".join(lines)


def _case_text(case: dict[str, Any]) -> str:
    """把一条用例的前置/后置相关文本拼成喂模型的描述。结构化 precondition（NL 列表）+ raw 原文都带上。"""
    raw = case.get("raw") or {}

    def _join(v: Any) -> str:
        if isinstance(v, list):
            return "；".join(str(x) for x in v)
        return str(v or "")

    pre = _join(case.get("precondition")) or _join(raw.get("precondition"))
    lines = [f"用例名称：{case.get('item', '')}"]
    if case.get("module"):
        lines.append(f"模块：{case['module']}")
    if case.get("order_type"):
        lines.append(f"订单类型标注：{case['order_type']}（以此为准填 create_order.with.order_type）")

    # 登录态前置：明确告知 AI 该用例需要什么登录态（logged_out / required / any）
    login_pre = case.get("login_precondition") or "required"
    login_pre_text = {
        "logged_out": "未登录/已登出",
        "required": "已登录",
        "any": "任意（登录或未登录均可）",
    }.get(login_pre, login_pre)
    lines.append(f"登录态前置：{login_pre_text}")

    lines.append(f"前置条件：{pre or '（未填）'}")
    # 正式步骤/预期一并给作上下文（帮模型理解用例意图），但 system 已交代它们不是前置/后置。
    if case.get("steps"):
        lines.append(f"测试步骤（仅作上下文）：{_join(case.get('steps'))}")
    if case.get("expects"):
        lines.append(f"预期结果（仅作上下文）：{_join(case.get('expects'))}")
    return "\n".join(lines)


def _validate_candidates(profile: AppProfile, refs: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """逐条过 resolve_operation 校验；返回 (保留的候选, 被丢弃的及原因)。

    保留的候选保形 {op, with, reason}（用户在两栏编辑器里要看 reason）；坏引用不污染用例。"""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for ref in refs or []:
        if not isinstance(ref, dict) or not ref.get("op"):
            dropped.append({"ref": repr(ref), "error": "候选形态非法（缺 op）"})
            continue
        op_name = str(ref["op"])
        with_args = ref.get("with")
        if with_args is not None and not isinstance(with_args, dict):
            dropped.append({"op": op_name, "error": f"with 必须是对象：{with_args!r}"})
            continue
        with_args = {k: v for k, v in (with_args or {}).items() if v is not None}
        try:
            ops_mod.resolve_operation(profile, op_name, with_args)
        except ops_mod.OperationError as exc:
            dropped.append({"op": op_name, "error": str(exc)})
            continue
        entry: dict[str, Any] = {"op": op_name}
        if with_args:
            entry["with"] = with_args
        if ref.get("reason"):
            entry["reason"] = str(ref["reason"])
        kept.append(entry)
    return kept, dropped


def _validate_hints(raw: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if raw in (None, {}):
        return {}, []
    if not isinstance(raw, dict):
        return {}, [{"hint": repr(raw), "error": "hints 必须是对象"}]
    allowed_keys = {"mode", "produce", "reason"}
    unknown = set(raw) - allowed_keys
    if unknown:
        return {}, [{"hint": repr(raw), "error": f"hints 含未知字段：{sorted(unknown)}"}]
    mode = str(raw.get("mode") or "").strip()
    if mode and mode not in {"read_only", "advance", "destructive"}:
        return {}, [{"hint": repr(raw), "error": f"mode 非法：{mode}"}]
    produce = str(raw.get("produce") or "").strip()
    if mode in {"advance", "destructive"} and not produce:
        return {}, [{"hint": repr(raw), "error": f"mode={mode} 必须带 produce"}]
    out: dict[str, Any] = {}
    if mode:
        out["mode"] = mode
    if produce:
        out["produce"] = produce
    if raw.get("reason"):
        out["reason"] = str(raw["reason"])
    return out, []


def suggest_for_case(
    app: str,
    case_id: str,
    *,
    profile: AppProfile | None = None,
    write: bool = True,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 180.0,
    runner: CommandRunner = _subprocess_runner,
) -> dict[str, Any]:
    """对单条用例做 AI 操作推荐，写回 `suggested_setup`/`suggested_teardown`（待确认）。

    返回 {ok, case_id, suggested_setup, suggested_teardown, dropped, provider, model, ...}。
    fail-closed：无操作库 / agent 没回结构化 JSON → ok=False，不写。write=False 只算不落库。
    """
    profile = profile or load_app_profile(app)
    cases_path = profile.cases_path()
    case = next((c for c in load_cases(cases_path) if str(c.get("id")) == case_id), None)
    if case is None:
        return {"ok": False, "error": f"case {case_id} not found in {cases_path}"}

    brief = _operation_brief(profile)
    if not brief.strip():
        return {"ok": False, "error": f"app {app} 操作库无 active 操作，无可推荐项"}

    prompt = (
        _case_text(case)
        + "\n\n可用操作清单（操作库 active 操作，name 须逐字引用）：\n"
        + brief
        + "\n\n按 schema 输出 setup / teardown 两组操作引用候选（没有合适的就留空数组）。"
    )
    req = AiRequest(
        prompt=prompt, system=_SYSTEM, tools="none", json_schema=_suggest_schema(profile),
        provider=provider, model=model, timeout_s=timeout_s,
    )
    res = invoke(req, runner=runner)
    if not res.ok or not isinstance(res.json, dict):
        return {"ok": False, "error": res.error or "suggest agent returned no structured json",
                "case_id": case_id, "provider": res.provider, "model": res.model}

    setup_kept, setup_dropped = _validate_candidates(profile, res.json.get("setup") or [])
    teardown_kept, teardown_dropped = _validate_candidates(profile, res.json.get("teardown") or [])
    hints_kept, hints_dropped = _validate_hints(res.json.get("hints"))

    out: dict[str, Any] = {
        "ok": True,
        "case_id": case_id,
        "suggested_setup": setup_kept,
        "suggested_teardown": teardown_kept,
        "dropped": setup_dropped + teardown_dropped,
        "hints": hints_kept,
        "dropped_hints": hints_dropped,
        "provider": res.provider,
        "model": res.model,
        "cost_usd": getattr(res, "cost_usd", None),
        "duration_s": getattr(res, "duration_s", None),
    }
    if write:
        changes: dict[str, Any] = {
            "suggested_setup": setup_kept,
            "suggested_teardown": teardown_kept,
        }
        if hints_kept:
            changes["raw.hints"] = hints_kept
        update_case(cases_path, case_id, changes)
    return out


def suggest_for_drafts(
    app: str,
    *,
    status: str = "draft",
    case_ids: list[str] | None = None,
    write: bool = True,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 180.0,
    runner: CommandRunner = _subprocess_runner,
) -> dict[str, Any]:
    """批量推荐：对 `status`（默认 draft）的用例，或显式 `case_ids`，逐条 suggest_for_case。

    返回 {ok, total, succeeded, results:[…], provider, model}。逐条 fail-soft（单条 AI 失败记进
    该条 result，不中断其余）。**ok 语义**：有目标却无一条成功（全失败）→ ok=False；无目标
    （total==0）或至少一条成功 → ok=True（review 547312e P2，避免全失败假报「推荐完成」）。
    """
    profile = load_app_profile(app)
    rows = load_cases(profile.cases_path())
    if case_ids is not None:
        wanted = {str(i) for i in case_ids}
        targets = [c for c in rows if str(c.get("id")) in wanted]
    else:
        targets = [c for c in rows if c.get("status") == status]

    results: list[dict[str, Any]] = []
    provider_seen = model_seen = None
    total_targets = len(targets)
    logger.info("suggest-ops 批量开始：app=%s status=%s 目标 %d 条", app, status, total_targets)
    for idx, c in enumerate(targets, 1):
        cid = str(c["id"])
        started = time.monotonic()
        logger.info("[%d/%d] 开始处理 %s", idx, total_targets, cid)
        try:
            r = suggest_for_case(
                app, cid, profile=profile, write=write,
                provider=provider, model=model, timeout_s=timeout_s, runner=runner,
            )
        except Exception as exc:  # noqa: BLE001 - 单条崩溃不拖垮整批，记为该条失败
            logger.exception("[%d/%d] %s 处理抛异常（记为失败，继续）", idx, total_targets, cid)
            r = {"ok": False, "case_id": cid, "error": f"{type(exc).__name__}: {exc}"}
        elapsed = round(time.monotonic() - started, 1)
        if r.get("ok"):
            logger.info("[%d/%d] %s ✅ (%.1fs) setup=%d teardown=%d dropped=%d",
                        idx, total_targets, cid, elapsed,
                        len(r.get("suggested_setup") or []), len(r.get("suggested_teardown") or []),
                        len(r.get("dropped") or []))
        else:
            logger.warning("[%d/%d] %s ❌ (%.1fs) %s",
                           idx, total_targets, cid, elapsed, r.get("error"))
        provider_seen = r.get("provider") or provider_seen
        model_seen = r.get("model") or model_seen
        results.append(r)
    total = len(results)
    succeeded = sum(1 for r in results if r.get("ok"))
    logger.info("suggest-ops 批量结束：%d/%d 成功", succeeded, total)
    return {
        # review 547312e P2：跑到底 ≠ 成功。有目标却无一条成功（全因无操作库/模型失败）→
        # ok=False，否则 CLI 退 0、AI job 报 done 会假报「推荐完成」。total==0（无目标）算成功
        # （没活干不是失败）；部分成功由调用方看 succeeded/total 区分。
        "ok": not (total > 0 and succeeded == 0),
        "total": total,
        "succeeded": succeeded,
        "results": results,
        "provider": provider_seen,
        "model": model_seen,
    }
