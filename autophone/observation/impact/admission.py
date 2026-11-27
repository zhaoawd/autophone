"""Effect Admission：Prod 设备执行的安全闸（影响图谱 §2.1-7 / §6.3 / §7.1）。

红线是「未获准写操作执行次数 = 0」。所以这里有两道独立的闸，缺一不可：

1. **声明闸**——operation 自己声明的 ``navigation.effects``（cart_mutation、place_order、
   payment…）必须逐条落在 profile 的 ``allowed_effects`` 里。profile 没声明 = 空 = 全拒。
2. **兜底闸**——扫 operation 的实际 steps，命中支付/提交订单/确认取消/立即领取/购买会员
   这类敏感 CTA 文案的，即便没声明 effect 也拒。声明可能漏，屏上的按钮不会。

两道闸都在 **planner 阶段**生效：计划里包含未获准 effect 的目标直接落 ``blocked`` +
``effect_not_admitted``，不进入执行；不能「先跑到那一步再说」——跑到那一步就已经晚了。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from autophone.execution import effects as effects_mod

# 影响图谱里会出现的 effect verb（§6.3）。不在这张表里的 verb 一样按未获准处理——
# 未知即拒，不是未知即放。
KNOWN_EFFECTS: frozenset[str] = frozenset({
    "cart_mutation",
    "address_mutation",
    "place_order",
    "payment",
    "cancel_order",
    "claim_coupon",
    "membership_purchase",
    "profile_mutation",
})

READ_ONLY = "read_only"
MUTATING = "mutating"

# 敏感 CTA 文案兜底表（§2.2 不自动做的那几件事 + §7.1 默认禁点）。
# 与 effects.irreversible_marker 互补：那张表管通用不可逆动词，这张管消费端的具体按钮。
SENSITIVE_CTA_MARKERS: tuple[str, ...] = (
    "提交订单", "去支付", "立即支付", "确认支付", "确认下单", "立即下单",
    "确认取消", "取消订单", "立即领取", "领取优惠", "立即购买", "开通会员",
    "立即续费", "续费", "确认兑换", "立即兑换", "确认删除", "解绑",
)

# 影响路径首步已经把这两个复位 executor 作为协议常量校验。将其 effect 同样固定为
# reversible，避免纯函数调用依赖进程中最后一次激活的 App executor 注册表。
TRUSTED_RESET_EXECUTORS: frozenset[str] = frozenset({
    "reset_to_home",
    "relaunch_home",
})


@dataclass(frozen=True)
class AdmissionDecision:
    decision: str  # read_only | admitted | not_admitted
    allowed_effects: tuple[str, ...]
    requested_effects: tuple[str, ...]
    denied_effects: tuple[str, ...]
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.decision in ("read_only", "admitted")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "decision": self.decision,
            "allowed_effects": list(self.allowed_effects),
            "requested_effects": list(self.requested_effects),
            "denied_effects": list(self.denied_effects),
        }
        if self.note:
            payload["note"] = self.note
        return payload


def decide(
    allowed_effects: Iterable[str],
    requested_effects: Iterable[str],
) -> AdmissionDecision:
    allowed = tuple(sorted({str(e) for e in allowed_effects if e}))
    requested = tuple(sorted({str(e) for e in requested_effects if e}))
    if not requested:
        return AdmissionDecision(READ_ONLY, allowed, requested, (), "计划内无写操作")
    denied = tuple(e for e in requested if e not in allowed)
    if denied:
        return AdmissionDecision(
            "not_admitted", allowed, requested, denied,
            f"未获准 effect：{list(denied)}；profile.allowed_effects={list(allowed)}",
        )
    return AdmissionDecision("admitted", allowed, requested, (), "全部 effect 已登记")


def sensitive_cta_marker(text: Any) -> str | None:
    """文案是否命中敏感 CTA。命中返回具体文案，供报告写清「因为哪个按钮被拒」。"""
    blob = str(text or "")
    if not blob:
        return None
    for marker in SENSITIVE_CTA_MARKERS:
        if marker in blob:
            return marker
    return None


def _step_texts(step: Any) -> list[str]:
    if isinstance(step, str):
        return [step]
    if not isinstance(step, dict):
        return []
    texts: list[str] = []
    for key in ("query", "label", "text", "target", "tap_query", "operation", "action"):
        value = step.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts


def scan_steps(steps: Sequence[Any]) -> list[dict[str, Any]]:
    """扫 operation steps，返回命中的敏感/不可逆动作证据。

    两个来源都查：影响图谱的敏感 CTA 文案表，以及平台通用的 ``effects`` 不可逆动词表
    （A3 effect policy 已经在用的那张）——两张表任一命中都算。
    """
    findings: list[dict[str, Any]] = []
    for idx, step in enumerate(steps or []):
        for text in _step_texts(step):
            marker = sensitive_cta_marker(text)
            if marker:
                findings.append({"step_index": idx, "marker": marker, "text": text, "source": "sensitive_cta"})
                continue
            verb = effects_mod.irreversible_marker(text)
            if verb:
                findings.append({"step_index": idx, "marker": verb, "text": text, "source": "irreversible_verb"})
    return findings


def _executor_decision(
    name: str,
    executor_effects: Mapping[str, effects_mod.EffectDecision] | None,
) -> effects_mod.EffectDecision:
    if executor_effects is not None and name in executor_effects:
        return executor_effects[name]
    if name in TRUSTED_RESET_EXECUTORS:
        return effects_mod.EffectDecision(
            effects_mod.REVERSIBLE,
            name,
            "trusted_reset_executor",
        )
    return effects_mod.EffectDecision(
        effects_mod.IRREVERSIBLE,
        name,
        "executor_missing_effect_declaration",
        declared=False,
    )


def _executor_declaration_finding(
    operation: Mapping[str, Any],
    *,
    executor_name: str,
    decision: effects_mod.EffectDecision,
    step_index: int | None,
    operation_path: Sequence[str],
) -> dict[str, Any] | None:
    raw_navigation = operation.get("navigation")
    navigation = dict(raw_navigation or {}) if isinstance(raw_navigation, Mapping) else {}
    declared_safety = str(navigation.get("safety") or "")
    declared_effects = [str(value) for value in navigation.get("effects") or [] if value]
    explicit = (
        isinstance(raw_navigation, Mapping)
        and "safety" in raw_navigation
        and "effects" in raw_navigation
    )
    reason: str | None = None
    if not decision.declared:
        reason = "executor effect 未注册"
    elif not explicit:
        reason = "引用 Python executor 的 YAML Operation 未显式声明 navigation.safety/effects"
    elif decision.irreversible and (
        declared_safety != MUTATING or not declared_effects
    ):
        reason = "写 executor 必须声明 safety=mutating 且至少一个 navigation.effect"
    elif declared_effects and declared_safety != MUTATING:
        reason = "声明了 navigation.effects 的 Operation 必须使用 safety=mutating"
    if reason is None:
        return None
    finding: dict[str, Any] = {
        "marker": decision.verb or executor_name,
        "text": executor_name,
        "source": "executor_effect_declaration",
        "reason": reason,
        "operation_path": list(operation_path),
    }
    if step_index is not None:
        finding["step_index"] = step_index
    return finding


def operation_safety(
    operation: dict[str, Any],
    *,
    operations: Mapping[str, Mapping[str, Any]] | None = None,
    executor_effects: Mapping[str, effects_mod.EffectDecision] | None = None,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """一条 operation → (safety, effects, 敏感证据)。

    safety 取三者最严：显式声明的 ``navigation.safety``、``navigation.effects`` 是否非空、
    以及 steps 里扫出来的敏感 CTA。任何一处说「有副作用」就是 mutating。
    """
    registry = operations or {}
    declared_effects: list[str] = []
    findings: list[dict[str, Any]] = []
    mutating_declared = False

    def add_effects(values: Iterable[Any]) -> None:
        for value in values:
            name = str(value or "")
            if name and name not in declared_effects:
                declared_effects.append(name)

    def visit(
        current: Mapping[str, Any],
        *,
        path: tuple[str, ...],
        stack: tuple[str, ...],
    ) -> None:
        nonlocal mutating_declared
        current_name = str(current.get("name") or path[-1])
        navigation = dict(current.get("navigation") or {})
        add_effects(navigation.get("effects") or [])
        if str(navigation.get("safety") or "") == MUTATING:
            mutating_declared = True

        own_executor = (
            executor_effects.get(current_name)
            if executor_effects is not None
            else None
        )
        if own_executor is not None:
            finding = _executor_declaration_finding(
                current,
                executor_name=current_name,
                decision=own_executor,
                step_index=None,
                operation_path=path,
            )
            if finding is not None:
                findings.append(finding)

        for index, step in enumerate(current.get("steps") or []):
            for text in _step_texts(step):
                marker = sensitive_cta_marker(text)
                if marker:
                    findings.append({
                        "step_index": index,
                        "marker": marker,
                        "text": text,
                        "source": "sensitive_cta",
                        "operation_path": list(path),
                    })
                    continue
                verb = effects_mod.irreversible_marker(text)
                if verb:
                    findings.append({
                        "step_index": index,
                        "marker": verb,
                        "text": text,
                        "source": "irreversible_verb",
                        "operation_path": list(path),
                    })
            if not isinstance(step, Mapping) or not step.get("operation"):
                continue
            reference = str(step["operation"])
            referenced = registry.get(reference)
            if referenced is not None and reference != current_name:
                if reference in stack:
                    findings.append({
                        "step_index": index,
                        "marker": reference,
                        "text": reference,
                        "source": "operation_reference_cycle",
                        "reason": "YAML Operation 引用形成循环",
                        "operation_path": list((*path, reference)),
                    })
                    continue
                visit(
                    referenced,
                    path=(*path, reference),
                    stack=(*stack, reference),
                )
                continue
            if reference == current_name and own_executor is not None:
                continue
            decision = _executor_decision(reference, executor_effects)
            finding = _executor_declaration_finding(
                current,
                executor_name=reference,
                decision=decision,
                step_index=index,
                operation_path=path,
            )
            if finding is not None:
                findings.append(finding)

    root_name = str(operation.get("name") or operation.get("id") or "<operation>")
    visit(operation, path=(root_name,), stack=(root_name,))
    safety = (
        MUTATING
        if mutating_declared or declared_effects or findings
        else READ_ONLY
    )
    return safety, declared_effects, findings


def finding_blocker(findings: Sequence[Mapping[str, Any]]) -> str:
    """把扫描证据映射为稳定 blocker；声明缺口与敏感 CTA 分开报告。"""
    if any(
        str(item.get("source") or "").startswith(
            ("executor_", "operation_reference_")
        )
        for item in findings
    ):
        return "effect_declaration_missing"
    return "sensitive_cta"


def route_effects(steps: Iterable[dict[str, Any]]) -> list[str]:
    """一条计划路径上的全部 effect（去重保序）。"""
    seen: list[str] = []
    for step in steps or []:
        for effect in step.get("effects") or []:
            name = str(effect)
            if name not in seen:
                seen.append(name)
    return seen
