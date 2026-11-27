"""familiarity.py — 「熟悉路径」状态来源（P5 agent lane，platform-ai-language-to-execution P5 边界）。

`ai_agent.classify_plan` 的 `familiar` 此前是**调用方手传的 bool**——没有真相源，等于「信任作者」，
正是本仓反复要消灭的那种不带证据的信任。本模块把它换成**证据派生**，纪律对齐 ADR-0003
「derived not tracked」：每次现算、绝不落到任何存储位（无 `familiar` 标志字段、无缓存写盘）。

判据（保守、单一信号、build-stamped）：一个 app「熟悉」当且仅当平台**在当前构建上**真把它驱动成功过——
最近回归里有 ≥1 条用例裁决 `passed`（`Service.latest_case_verdicts`），且该裁决所在 Run 的构建身份
等于当前构建。旧构建的 passed 不算（升级后界面或路径可能变化）；Run 未记录构建身份、或设备侧
构建比对为 `mismatch` 的 passed 也不算。没有当前构建的成功史 → 不熟悉 → agent lane 整盘预批。

构建身份比对（BL-067 2b）：
- 当前构建：`build_id` 取当前 Target 安装回执的 Build Record（与 Run 提交时冻结 `installed_build_ref`
  同一来源；回执 `verified=false` 时不用）；`build_label` 取 `profile.build_label`（区分力闸
  `discrimination_gaps(current_build_label=)` 的比对基准）。
- 逐条 passed 裁决：双方都有 `build_id` 时按 `build_id` 比；否则双方都有 `build_label` 时按
  `build_label` 比；都不可比 → 构建不明。
- 当前构建身份未知（无可用回执且 profile 无 build_label）→ 无法判断新旧，退回宽松判据（任一 passed
  即熟悉），reason 标明「未按构建区分」。

这与 blast-radius 脊一致：familiarity 只决定「需不需要整盘预批」，**不替代** `classify_action`
的不可逆/对外闸——不可逆动作无论 app 熟不熟，仍走 checkpoint 待批。

辅助证据（不改判据、只丰富 reason）：app 是否有 active 操作带真执行器（login/create_order/
scan_duty 或纯 typed-IR），即平台「有手段」驱动它。见 [[live-vm-boundaries-proven]]。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from autophone.assets.apps import AppProfile, load_app_profile
from autophone.execution.operation_run import has_builtin_executor, is_typed_ir_steps
from autophone.assets.operations import load_operations

ReceiptLoader = Callable[[AppProfile], "Mapping[str, Any] | None"]


@dataclass
class Familiarity:
    """派生的熟悉度。`familiar` 喂 `classify_plan`；`reason`/`evidence` 供人审与面板可核对。"""
    familiar: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _op_has_executor(name: str, op: dict[str, Any], *, app: str | None = None) -> bool:
    """该 active 操作是否平台可真驱动：有内置执行器，或 steps 是可被 dispatcher 合成执行的 typed IR。"""
    return has_builtin_executor(name, app=app) or is_typed_ir_steps(op.get("steps"))


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _target_receipt(profile: AppProfile) -> Mapping[str, Any] | None:
    from autophone.packages import store as packages

    return packages.load_target_receipt(profile.app, root=profile.host_state_root)


def current_build_identity(
    profile: AppProfile, *, receipt_loader: ReceiptLoader | None = None,
) -> dict[str, str | None]:
    """当前构建身份 `{build_id, build_label}`，缺失项为 None。只读，不探测设备。"""
    build_id = None
    try:
        receipt = (receipt_loader or _target_receipt)(profile)
    except Exception:  # noqa: BLE001 - 回执缺失或损坏按「无 build_id」处理，退到 build_label
        receipt = None
    if isinstance(receipt, Mapping) and receipt.get("verified") is not False:
        build_id = _text((receipt.get("build_ref") or {}).get("build_id"))
    return {"build_id": build_id, "build_label": _text(getattr(profile, "build_label", None))}


def _match_basis(verdict: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, str | None]:
    """一条裁决相对当前构建的归属：("current"|"other"|"unknown", 比对依据)。"""
    if verdict.get("build_identity_match") == "mismatch":
        return "unknown", None
    run_id, cur_id = _text(verdict.get("build_id")), current.get("build_id")
    if run_id and cur_id:
        return ("current" if run_id == cur_id else "other"), "build_id"
    run_label, cur_label = _text(verdict.get("build_label")), current.get("build_label")
    if run_label and cur_label:
        return ("current" if run_label == cur_label else "other"), "build_label"
    return "unknown", None


def partition_passed(
    verdicts: Mapping[str, Mapping[str, Any]], current: Mapping[str, Any] | None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """把 passed 裁决按构建分桶：(当前构建 {case_id: 比对依据}, 旧构建, 构建不明)。

    `current` 为 None 或两项皆空 → 不分构建，全部 passed 记入当前桶（依据 "unscoped"）。
    """
    passed = {cid: v for cid, v in verdicts.items() if v.get("status") == "passed"}
    if not current or not (current.get("build_id") or current.get("build_label")):
        return {cid: "unscoped" for cid in passed}, [], []
    cur: dict[str, str] = {}
    other: list[str] = []
    unknown: list[str] = []
    for cid, verdict in passed.items():
        bucket, basis = _match_basis(verdict, current)
        if bucket == "current":
            cur[cid] = str(basis)
        elif bucket == "other":
            other.append(cid)
        else:
            unknown.append(cid)
    return cur, other, unknown


def _describe(current: Mapping[str, Any]) -> str:
    parts = []
    if current.get("build_id"):
        parts.append(f"build_id={str(current['build_id'])[:16]}…")
    if current.get("build_label"):
        parts.append(f"build_label={current['build_label']}")
    return "，".join(parts)


def derive_familiarity(
    *,
    passed_cases: Iterable[str],
    executor_ops: Iterable[str],
    current_build: Mapping[str, Any] | None = None,
    passed_other_build: Iterable[str] = (),
    passed_unknown_build: Iterable[str] = (),
    passed_match_basis: Mapping[str, str] | None = None,
) -> Familiarity:
    """纯函数：从证据派生熟悉度。判据单一——当前构建上有成功驱动史（passed_cases 非空）即熟悉。

    `passed_cases` 是当前构建上的 passed；`current_build` 为 None 或两项皆空时表示未按构建区分，
    此时 `passed_cases` 是全部 passed（宽松判据）。旧构建/构建不明的 passed 只进 evidence 与 reason。
    executor_ops 只进 evidence/reason（「有手段」的佐证），不参与判据：有执行器但从没跑成
    不构成「熟悉」（避免「信任能力声明」而非「信任证据」）。
    """
    passed = sorted(passed_cases)
    other = sorted(passed_other_build)
    unknown = sorted(passed_unknown_build)
    ops = sorted(executor_ops)
    scoped = bool(current_build and (current_build.get("build_id") or current_build.get("build_label")))
    evidence: dict[str, Any] = {
        "build_scoped": scoped,
        "current_build": dict(current_build) if current_build else None,
        "passed_cases": passed,
        "passed_match_basis": dict(passed_match_basis or {}),
        "passed_cases_other_build": other,
        "passed_cases_unknown_build": unknown,
        "executor_ops": ops,
    }
    means = f"；有手段：{len(ops)} 条 active 操作带执行器" if ops else ""
    if passed:
        sample = ", ".join(passed[:3])
        if scoped:
            reason = (f"平台在当前构建（{_describe(current_build or {})}）上成功驱动过 "
                      f"{len(passed)} 条用例（如 {sample}）{means}")
        else:
            reason = (f"平台最近成功驱动过 {len(passed)} 条用例（如 {sample}）；当前构建身份未知，"
                      f"未按构建区分{means}")
        return Familiarity(True, reason, evidence)
    if other or unknown:
        stale = []
        if other:
            stale.append(f"{len(other)} 条旧构建")
        if unknown:
            stale.append(f"{len(unknown)} 条构建不明")
        reason = (f"当前构建（{_describe(current_build or {})}）上无成功驱动史，仅有"
                  f"{'、'.join(stale)}的 passed → 不熟悉，整盘预批")
        return Familiarity(False, reason, evidence)
    reason = ("无成功驱动史（最近回归无 passed 用例）→ 不熟悉，整盘预批"
              + (f"（虽有 {len(ops)} 条带执行器的操作，但未证跑成）" if ops else ""))
    return Familiarity(False, reason, evidence)


def app_familiarity(
    app: str | None,
    *,
    profile: AppProfile | None = None,
    service: Any = None,
    receipt_loader: ReceiptLoader | None = None,
) -> Familiarity:
    """gatherer：采集 build-stamped 证据并派生。app 为空/未知 → 不熟悉（无证据）。

    `service` 注入接缝（默认懒建 `RunService`，避免顶层 import 环 + 让单测免文件系统）；
    `receipt_loader` 注入当前 Target 安装回执（默认读 Package Store，只读）。
    """
    if not app:
        return Familiarity(False, "未指定 app → 不熟悉", {})
    try:
        profile = profile or load_app_profile(app)
    except Exception:  # noqa: BLE001 - 未知 app 是 agent lane 合法常态，按不熟悉处理
        return Familiarity(False, f"未知 app「{app}」→ 不熟悉", {})

    if service is None:
        from autophone.execution.run_service import RunService
        service = RunService()
    verdicts = service.latest_case_verdicts(app)
    current = current_build_identity(profile, receipt_loader=receipt_loader)
    passed_current, passed_other, passed_unknown = partition_passed(verdicts, current)

    ops = load_operations(profile)
    executor_ops = [name for name, op in ops.items()
                    if str(op.get("status")) == "active" and _op_has_executor(name, op, app=profile.app)]

    return derive_familiarity(
        passed_cases=list(passed_current),
        executor_ops=executor_ops,
        current_build=current,
        passed_other_build=passed_other,
        passed_unknown_build=passed_unknown,
        passed_match_basis={cid: b for cid, b in passed_current.items() if b != "unscoped"},
    )
