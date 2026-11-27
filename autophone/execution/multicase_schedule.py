"""Read-only multicase scheduling analysis helpers.

This module intentionally stays above the runner.  Phase 0/1/5 need numbers and
metadata before the execution engine is allowed to guess about sharing.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from autophone.paths import APP_ASSET_ROOT, STATE_ROOT

ROOT = APP_ASSET_ROOT
from autophone.assets.apps import AppProfile, load_app_profile
from autophone.execution.case_selection import CaseSelector, select_cases
from autophone.assets.scheduling import contract_for_case, setup_refs

# create_order 走自己专门的 opt-in 闸（runner._order_pool_sig 要求显式
# order_consumes: false），不该被声明前缀共享（runner._declared_prefix_share）零门槛顺手
# 捎带——同一操作两套安全线不一致会让「未显式 opt-in 的 create_order」照样被当共享前缀跳过
# （见 review 43e3cbf2 finding #4）。归进 polluting 就切断它进 shareable_prefix 的资格。
# 加购与减购都改变购物车；即便减购让数量变小，也不能视作可共享的纯前置。
POLLUTING_OPERATIONS = {
    "add_cart_amount", "add_snack", "add_to_cart", "decrease_cart_item", "clear_cart", "create_order",
}
CLEANUP_OPERATIONS = {"reset_to_home", "logout", "clear_cart", "cleanup_cart"}
# 订单池 M2 advisory hint 的合法 mode 三值（regression-order-precondition-pool.md §十.2 定盘
# 2026-07-05）。只用于报告侧校验回显；M2 开工前 runner 不读这两个 hint。
ORDER_HINT_MODES = ("read_only", "advance", "destructive")
PHASE5_BACKLOG = (
    "T2a_stable_clustering",
    "T2b_read_only_first",
    "T2c_state_chain_alignment",
    "navigation_dfs_lca",
    "hit_miss_pairing",
)


def load_cases_doc(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_cases(path: Path) -> list[dict[str, Any]]:
    doc = load_cases_doc(path)
    return [c for c in (doc.get("cases") or []) if isinstance(c, dict)]


def setup_ops(case: dict[str, Any]) -> list[str]:
    return [str(s["op"]) for s in setup_refs(case)]


def norm_args(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(sorted((str(k), repr(v)) for k, v in value.items()))


def setup_signature(case: dict[str, Any]) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    return tuple((str(ref["op"]), norm_args(ref.get("with") or {})) for ref in setup_refs(case))


def setup_prefix_key(case: dict[str, Any]) -> str:
    ops = setup_ops(case)
    return " > ".join(ops) if ops else "(none)"


def _operation_steps(steps: list[dict[str, Any]]) -> list[str]:
    return [
        str(step["operation"])
        for step in (steps or [])
        if isinstance(step, dict) and step.get("operation")
    ]


def _expect_steps(api_expectations: dict[str, Any] | None) -> set[str]:
    steps: set[str] = set()
    if not isinstance(api_expectations, dict):
        return steps
    for section in ("expect", "forbid"):
        for entry in api_expectations.get(section) or []:
            if isinstance(entry, dict) and entry.get("step"):
                steps.add(str(entry["step"]))
    return steps


def _step_args(step: dict[str, Any]) -> dict[str, Any]:
    args = step.get("args")
    return args if isinstance(args, dict) else {}


def _row_text(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    return "\n".join(str(v) for v in row.values() if v not in (None, ""))


def _normalize_group_text(value: str) -> str:
    text = re.sub(r"\s+", "", value)
    text = re.sub(r"(满足|不满足|享受|不享受|命中|未命中|可用|不可用|成功|失败)", "", text)
    text = re.sub(r"(还差|达到|超过|低于|高于)", "", text)
    return text[:80]


def infer_group_key(steps: list[dict[str, Any]], row: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Report-only Phase 1b grouping hint.

    This deliberately stays fuzzy and advisory: it helps dry-run reports show
    likely same-promotion/same-store clusters, but no runner path consumes it.
    """
    text = _row_text(row)
    store = ""
    for step in steps:
        if not isinstance(step, dict) or step.get("operation") not in {
            "select_store", "select_store_takeout",
        }:
            continue
        args = _step_args(step)
        if args.get("store"):
            store = str(args["store"])
            break

    promotion = ""
    item = str((row or {}).get("item") or "")
    business_rule = str((row or {}).get("business_rule") or "")
    if any(k in text for k in ("优惠", "促销", "满减", "买一送一", "第二件", "券")):
        promotion = _normalize_group_text(business_rule or item)

    out = {k: v for k, v in {"store": store, "promotion": promotion}.items() if v}
    return out or None


def infer_order_hints(
    app: str,
    steps: list[dict[str, Any]],
    row: dict[str, Any] | None = None,
    api_expectations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report-only order-pool hints plus a conservative M1 prefill signal.

    The stable execution opt-in remains ``order_consumes: false``.  This helper
    only proposes that opt-in for obvious read-only order-view cases; ambiguous
    lifecycle verbs stay destructive by default.
    """
    del app  # hints depend only on the declared operations and row text
    ops = _operation_steps(steps)
    if "create_order" not in ops:
        return {}
    if "create_order" in _expect_steps(api_expectations):
        return {}
    text = _row_text(row)
    destructive = any(k in text for k in (
        "取餐", "送达", "完成", "取消订单", "取消配送", "接单", "改派", "退单", "上报异常",
    ))
    read_only = any(k in text for k in (
        "查看", "验证", "展示", "显示", "列表", "详情", "拨号", "导航", "发送信息", "刷新",
    ))
    if read_only and not destructive:
        return {"mode": "read_only", "order_consumes": False}
    return {"mode": "destructive"}


def build_role_hints(
    steps: list[dict[str, Any]],
    api_expectations: dict[str, Any] | None = None,
    *,
    app: str = "",
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Phase 1a/1b role hints for a converted case.

    The hints are deliberately advisory.  They live in ``raw.hints`` and are
    safe to ignore; missing hints must keep the runner conservative.
    """
    ops = _operation_steps(steps)
    coupled = _expect_steps(api_expectations)
    first_coupled_index = min((i for i, op in enumerate(ops) if op in coupled), default=len(ops))
    roles: list[dict[str, Any]] = []
    shareable_prefix: list[str] = []
    prefix_open = True

    for i, op in enumerate(ops):
        is_coupled = op in coupled
        if op in CLEANUP_OPERATIONS:
            role = "cleanup"
            category = "cleanup_state"
        elif is_coupled:
            role = "body"
            category = "assertion_trigger"
        elif op in POLLUTING_OPERATIONS:
            role = "setup"
            category = "polluting_state"
        elif i < first_coupled_index:
            role = "setup"
            category = "shareable_precondition_candidate"
        else:
            role = "body"
            category = "uncoupled_body_driver"

        if prefix_open and category == "shareable_precondition_candidate":
            shareable_prefix.append(op)
        else:
            prefix_open = False

        roles.append({
            "operation": op,
            "role": role,
            "category": category,
            "expect_coupled": is_coupled,
        })

    hints: dict[str, Any] = {
        "role_hint": roles,
        "expect_coupled_operations": sorted(op for op in coupled if op in ops),
    }
    if shareable_prefix:
        hints["shareable_prefix"] = shareable_prefix
    polluting = [r["operation"] for r in roles if r["category"] == "polluting_state"]
    if polluting:
        hints["polluting_state"] = polluting
    group_key = infer_group_key(steps, row)
    if group_key:
        hints["group_key"] = group_key
    hints.update({k: v for k, v in infer_order_hints(
        app, steps, row, api_expectations,
    ).items() if k != "order_consumes"})
    return hints


def suite_profile_from_convert_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    prefixes: Counter[tuple[str, ...]] = Counter()
    prefix_cases: defaultdict[tuple[str, ...], list[str]] = defaultdict(list)
    missing_unlocks: Counter[str] = Counter()
    coupled: Counter[str] = Counter()
    polluting: Counter[str] = Counter()
    group_keys: Counter[str] = Counter()
    group_cases: defaultdict[str, list[str]] = defaultdict(list)

    for result in results:
        if not result.get("ok"):
            continue
        case_id = str(result.get("case_id") or result.get("external_id") or result.get("item") or "?")
        hints = result.get("hints") or ((result.get("case") or {}).get("raw") or {}).get("hints") or {}
        prefix = tuple(str(op) for op in (hints.get("shareable_prefix") or []) if op)
        if prefix:
            prefixes[prefix] += 1
            prefix_cases[prefix].append(case_id)
        for op in set(str(op) for op in (result.get("missing_operations") or [])):
            missing_unlocks[op] += 1
        for op in hints.get("expect_coupled_operations") or []:
            coupled[str(op)] += 1
        for op in hints.get("polluting_state") or []:
            polluting[str(op)] += 1
        group_key = hints.get("group_key")
        if isinstance(group_key, dict) and group_key:
            key = json.dumps(group_key, ensure_ascii=False, sort_keys=True)
            group_keys[key] += 1
            group_cases[key].append(case_id)

    prefix_rows = []
    for prefix, count in prefixes.most_common():
        prefix_rows.append({
            "prefix": list(prefix),
            "cases": count,
            "estimated_adjacent_saving_upper_bound": max(0, count - 1),
            "sample_case_ids": prefix_cases[prefix][:10],
        })

    return {
        "shareable_prefixes": prefix_rows,
        "missing_operation_unlocks": [
            {"operation": op, "unlock_count": count}
            for op, count in missing_unlocks.most_common()
        ],
        "expect_coupled_operations": dict(coupled.most_common()),
        "polluting_operations": dict(polluting.most_common()),
        "group_keys": [
            {
                "key": json.loads(key),
                "cases": count,
                "sample_case_ids": group_cases[key][:10],
            }
            for key, count in group_keys.most_common()
        ],
        "phase5_backlog": phase5_backlog(prefix_rows),
    }


def phase5_backlog(prefix_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    savings = sum(int(row.get("estimated_adjacent_saving_upper_bound") or 0)
                  for row in (prefix_rows or []))
    return [
        {
            "item": item,
            "status": "backlog",
            "eligible": False,
            "reason": (
                "requires suite-profile savings plus order_independent governance; "
                f"current_prefix_saving_upper_bound={savings}"
            ),
        }
        for item in PHASE5_BACKLOG
    ]


def case_library_profile(
    cases: list[dict[str, Any]],
    *,
    actual_selector: CaseSelector | dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_all = select_cases(cases, {"statuses": ["active"]})
    actual = select_cases(cases, actual_selector or {"statuses": ["active"], "tag": "ui_ready"})

    def _scope(rows: list[dict[str, Any]]) -> dict[str, Any]:
        status_counts = Counter(str(c.get("status") or "active") for c in rows)
        prefix_counts = Counter(setup_prefix_key(c) for c in rows)
        sig_counts = Counter(repr(setup_signature(c)) for c in rows if setup_signature(c))
        adjacent_savings = 0
        full_cluster_savings = 0
        prev_sig: tuple | None = None
        seen: Counter[tuple] = Counter()
        for c in rows:
            sig = setup_signature(c)
            if sig and sig == prev_sig:
                adjacent_savings += 1
            if sig:
                seen[sig] += 1
            prev_sig = sig if sig else None
        full_cluster_savings = sum(max(0, count - 1) for count in seen.values())
        return {
            "total": len(rows),
            "statuses": dict(status_counts.most_common()),
            "setup_prefixes": dict(prefix_counts.most_common()),
            "setup_signatures": dict(sig_counts.most_common(20)),
            "adjacent_reuse_upper_bound": adjacent_savings,
            "cluster_reuse_upper_bound": full_cluster_savings,
            "reorder_marginal_upper_bound": max(0, full_cluster_savings - adjacent_savings),
        }

    return {
        "active_all": _scope(active_all),
        "actual_selector": _scope(actual),
    }


def _raw_hints(case: dict[str, Any]) -> dict[str, Any]:
    raw = case.get("raw") if isinstance(case.get("raw"), dict) else {}
    hints = raw.get("hints") if isinstance(raw.get("hints"), dict) else {}
    return hints


def _expect_coupled_ops(case: dict[str, Any]) -> set[str]:
    return set(contract_for_case(case).expect_coupled_operations)


def _order_pool_eligible(case: dict[str, Any]) -> bool:
    """镜像 runner._order_pool_sig 的 M1 opt-in 判定（不 import runner——本模块刻意留在
    执行引擎之上，只读画像不拉执行依赖）：显式 order_consumes: false、无 teardown、
    create_order 未被 expect 静态闸咬住。runner 侧判定变了这里要同步（有护栏测试对拍）。"""
    return (
        case.get("order_consumes") is False
        and not case.get("teardown")
        and "create_order" not in _expect_coupled_ops(case)
    )


def order_pool_profile(
    cases: list[dict[str, Any]],
    *,
    actual_selector: CaseSelector | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """订单池 Phase 0 只读度量（regression-order-precondition-pool.md §九 M1 开工门）。

    回答「订单侧值不值」的底数：单层 `op:create_order` 口径命中了谁（定盘写实：当前
    active 预期只有 R130）、其中多少已 opt-in 只读、同身份（create_order `with` 参数
    规范化，与 runner._order_pool_sig 同派生）能省几次造单、双层栈（R117 类
    `[login, create_order]`）被 M1 口径排除的名单。双口径输出同 case_library_profile。

    `advisory_hints` 段回显 `raw.hints` 里的 `mode`/`produce` 并做一致性检查——
    **报告侧 only**。runner 只消费人审后的稳定 `case.scheduling.order_pool`。
    """
    def _identity(case: dict[str, Any]) -> str:
        return repr(norm_args(setup_refs(case)[0].get("with") or {}))

    def _scope(rows: list[dict[str, Any]]) -> dict[str, Any]:
        single: list[dict[str, Any]] = []
        multi_excluded: list[dict[str, Any]] = []
        for c in rows:
            ops = setup_ops(c)
            if "create_order" not in ops:
                continue
            if len(ops) == 1:
                single.append(c)
            else:
                multi_excluded.append({"id": str(c.get("id")), "setup": ops})
        opt_in = [c for c in single if _order_pool_eligible(c)]

        def _reuse_upper(subset: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
            seen = Counter(_identity(c) for c in subset)
            return sum(max(0, n - 1) for n in seen.values()), dict(seen.most_common())

        opt_in_saving, _ = _reuse_upper(opt_in)
        potential_saving, identities = _reuse_upper(single)
        return {
            "single_layer_total": len(single),
            "single_layer_case_ids": [str(c.get("id")) for c in single][:20],
            "opt_in_read_only": len(opt_in),
            "opt_in_case_ids": [str(c.get("id")) for c in opt_in][:20],
            # 池 M1 能省的造单次数上限：同身份条数 −1 求和。current = 只算已 opt-in 的；
            # if_all = 假设单层口径全部声明只读后的天花板（决定还值不值得推动 opt-in）。
            "reuse_upper_bound_current_opt_in": opt_in_saving,
            "reuse_upper_bound_if_all_opt_in": potential_saving,
            "identities": identities,
            "multi_layer_excluded_total": len(multi_excluded),
            "multi_layer_excluded": multi_excluded[:20],
        }

    declared: list[dict[str, Any]] = []
    for c in cases:
        if "create_order" not in setup_ops(c):
            continue
        hints = _raw_hints(c)
        mode, produce = hints.get("mode"), hints.get("produce")
        if mode is None and produce is None:
            continue
        advisories: list[str] = []
        if mode is not None and str(mode) not in ORDER_HINT_MODES:
            advisories.append("invalid_mode")
        if mode == "read_only" and c.get("order_consumes") is not False:
            # M1 只读稳定字段 order_consumes——hint 不能替它 opt-in（§十.2 两级定盘）
            advisories.append("m1_requires_order_consumes_false")
        if mode == "advance" and not produce:
            # M2 release-readback 缺待证目标态 → 只能 fail-closed 逐出，归档收益为零（§五 定盘补）
            advisories.append("advance_without_produce")
        if produce and mode != "advance":
            advisories.append("produce_without_advance")
        if c.get("order_consumes") is False and mode in ("advance", "destructive"):
            advisories.append("conflicting_consumes")
        declared.append({
            "id": str(c.get("id")),
            "status": str(c.get("status") or "active"),
            "mode": mode,
            "produce": produce,
            "advisories": advisories,
        })

    return {
        "active_all": _scope(select_cases(cases, {"statuses": ["active"]})),
        "actual_selector": _scope(select_cases(
            cases, actual_selector or {"statuses": ["active"], "tag": "ui_ready"})),
        "advisory_hints": {
            "declared": declared,
            "note": "mode/produce raw.hints 仅供建议；runner 只消费 case.scheduling.order_pool",
        },
    }


def load_active_expectation_steps(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, set[str]] = {}
    for exp in doc.get("expectations") or []:
        if not isinstance(exp, dict) or exp.get("status") != "active" or not exp.get("id"):
            continue
        steps = _expect_steps(exp)
        if steps:
            out[str(exp["id"])] = steps
    return out


def setup_expect_coupling_profile(cases: list[dict[str, Any]], expectation_steps: dict[str, set[str]]) -> dict[str, Any]:
    coupled_cases: list[dict[str, Any]] = []
    counter: Counter[str] = Counter()
    for case in select_cases(cases, {"statuses": ["active"]}):
        ops = set(setup_ops(case))
        hit = sorted(ops & expectation_steps.get(str(case.get("id")), set()))
        if not hit:
            continue
        counter.update(hit)
        coupled_cases.append({"id": case.get("id"), "setup_coupled_operations": hit})
    return {
        "case_count": len(coupled_cases),
        "operations": dict(counter.most_common()),
        "cases": coupled_cases[:50],
    }


def _is_negative_control_run(run_dir: Path) -> bool:
    try:
        spec = json.loads((run_dir / "run-spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(spec, dict) and spec.get("negative_control") is not None


def summarize_run_artifacts(
    run_root: Path, profile: AppProfile, cases: list[dict[str, Any]], *, limit: int = 20,
) -> dict[str, Any]:
    """`cases` 由调用方传入（build_phase0_report 已 load_cases 过一次）——这里不再重读同一份
    cases yaml（review 43e3cbf2 finding #7）。

    run 目录名本身以时间戳打头（`runs/platform/<date>/<run_id>/`，run_id 前缀即时间戳），
    按路径字符串倒序即时间倒序，省下对每个候选文件的 stat() 调用；真正的「只看最近 limit
    个」仍在下面的读取+解析循环里生效（glob 本身仍需枚举全部候选路径才能知道谁最新——这一
    步没有额外索引不可省，但至少不再为每个文件多打一次 stat）。
    """
    cases_by_id = {str(c.get("id")): c for c in cases}
    candidates = sorted(run_root.glob("*/*/platform-run-result.json"),
                       key=lambda p: str(p.parent), reverse=True)

    runs = 0
    setup_action_ms: Counter[str] = Counter()
    setup_action_count: Counter[str] = Counter()
    synthetic_reuse = Counter()
    run_samples: list[str] = []
    for platform_path in candidates:
        if runs >= limit:
            break
        try:
            platform = json.loads(platform_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if platform.get("app") != profile.app or platform.get("type") not in (None, "regression"):
            continue
        if _is_negative_control_run(platform_path.parent):
            continue  # 区分力反向 Run 执行扰动后的 Recipe，不代表回归的前置耗时
        result_path = platform_path.parent / "result.json"
        if not result_path.exists():
            continue
        try:
            phase2 = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs += 1
        run_samples.append(str(platform_path.parent))
        for outcome in phase2.get("cases") or []:
            case = cases_by_id.get(str(outcome.get("id"))) or {}
            setup_names = set(setup_ops(case))
            for action in outcome.get("actions") or []:
                name = str(action.get("action") or "")
                elapsed = float(action.get("elapsed_ms") or 0)
                if name in setup_names:
                    setup_action_ms[name] += elapsed
                    setup_action_count[name] += 1
                if name.endswith("_reused") or name == "setup_prefix_reused":
                    synthetic_reuse[name] += 1

    return {
        "runs_scanned": runs,
        "sample_run_dirs": run_samples[:10],
        "setup_action_ms": {k: round(v, 1) for k, v in setup_action_ms.most_common()},
        "setup_action_count": dict(setup_action_count.most_common()),
        "synthetic_reuse_actions": dict(synthetic_reuse.most_common()),
    }


def build_phase0_report(
    apps: list[str],
    *,
    root: Path = ROOT,
    run_root: Path | None = None,
    actual_selector: CaseSelector | dict[str, Any] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    run_base = run_root or ((STATE_ROOT if root == APP_ASSET_ROOT else root) / "runs" / "platform")
    app_reports: dict[str, Any] = {}
    for app in apps:
        profile = load_app_profile(app, root=root)
        cases = load_cases(profile.cases_path())
        runs_summary = summarize_run_artifacts(run_base, profile, cases, limit=limit)
        order_pool = order_pool_profile(cases, actual_selector=actual_selector)
        # M1 开工门要回答的钱数：实测造单均值 × 池能省的造单次数上限（§九 定盘 2026-07-05）。
        # 上限口径：未扣交付前 readback 成本（§十.3 要求另量，净赚判定在那之后才有资格下）。
        total_ms = float(runs_summary["setup_action_ms"].get("create_order", 0.0))
        samples = int(runs_summary["setup_action_count"].get("create_order", 0))
        avg_ms = round(total_ms / samples, 1) if samples else None

        def _est(bound: int) -> float | None:
            return round(avg_ms * bound, 1) if avg_ms is not None else None

        order_pool["measured_create_order"] = {"avg_ms": avg_ms, "samples": samples}
        order_pool["estimated_saving_ms"] = {
            "actual_selector_current_opt_in": _est(
                order_pool["actual_selector"]["reuse_upper_bound_current_opt_in"]),
            "actual_selector_if_all_opt_in": _est(
                order_pool["actual_selector"]["reuse_upper_bound_if_all_opt_in"]),
            "active_all_if_all_opt_in": _est(
                order_pool["active_all"]["reuse_upper_bound_if_all_opt_in"]),
            "note": "上限=造单均值×可省次数；未扣 readback 交付闸成本（§十.3 需另量）",
        }
        app_reports[app] = {
            "cases": case_library_profile(cases, actual_selector=actual_selector),
            "setup_expect_coupling": setup_expect_coupling_profile(
                cases,
                load_active_expectation_steps(profile.api_expectations_path()),
            ),
            "order_pool": order_pool,
            "runs": runs_summary,
        }
    return {
        "ok": True,
        "phase": "Phase 0",
        "run_root": str(run_base),
        "apps": app_reports,
    }
