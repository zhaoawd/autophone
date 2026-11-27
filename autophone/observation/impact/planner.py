"""影响闭包与路径规划（影响图谱 §11.1 / §11.2 / §7.4）。

    changed config/data keys
      → changed endpoint fields
      → active causal/correlated bindings
      → affected ScreenVariants + regions
      → required ConsumerContexts
      → safe operation paths

规划只用**已注册的 active operation** 组路径——不猜控件、不盲爬。找不到路径时输出
``route_unavailable`` 而不是「大概点几下能到」：一个诚实的 gap 比一条编出来的路径有用。

置信度决定执行姿态（§11.1）：causal 默认执行，correlated 只作 advisory，observed 仅列
候选，stale 一律不执行、回到待标定。
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from autophone.observation.impact import admission as admission_mod
from autophone.observation.impact import bindings as bindings_mod
from autophone.observation.impact import context as ctx_mod
from autophone.observation.impact import endpoints as endpoint_mod
from autophone.observation.impact import manifest as manifest_mod
from autophone.observation.impact import policy as policy_mod
from autophone.contracts import validate_or_raise
from autophone.util import now_iso

# 冷启动复位后的根页面（§7.3 第 1 组路径）
DEFAULT_START_SCREEN = "home"

EXECUTION_BY_CONFIDENCE = {
    bindings_mod.CAUSAL: "execute",
    bindings_mod.CORRELATED: "advisory",
    bindings_mod.OBSERVED: "candidate",
    bindings_mod.STALE: "blocked",
}
START_RESET_OPERATIONS = frozenset({"reset_to_home", "relaunch_home"})


@dataclass(frozen=True)
class NavEdge:
    operation: str
    revision: int
    from_screen: str | None  # None = 任意起点
    to_screen: str
    safety: str
    effects: tuple[str, ...]
    requires: dict[str, Any]
    sensitive: tuple[dict[str, Any], ...] = ()

    def as_step(self) -> dict[str, Any]:
        step: dict[str, Any] = {
            "operation": self.operation,
            "revision": self.revision,
            "to": self.to_screen,
        }
        if self.from_screen:
            step["from"] = self.from_screen
        step["safety"] = self.safety
        if self.effects:
            step["effects"] = list(self.effects)
        return step


class OperationGraph:
    """active operation 的 ``navigation`` 段构成的有向图。

    只有声明了 ``navigation.to`` 且有目标 Screen checkpoint 的 active operation 才是边。
    ``from == to`` 的自环不进入普通 BFS；只有同时满足复位、checkpoint、只读和零 effect
    的自环可作为起始 Screen 验证器。
    """

    def __init__(
        self,
        edges: Iterable[NavEdge],
        *,
        verifiers: Iterable[NavEdge] = (),
    ) -> None:
        self._edges = list(edges)
        self._verifiers = list(verifiers)
        self._out: dict[str | None, list[NavEdge]] = {}
        for edge in self._edges:
            self._out.setdefault(edge.from_screen, []).append(edge)

    @property
    def edges(self) -> list[NavEdge]:
        return list(self._edges)

    @property
    def screens(self) -> set[str]:
        out: set[str] = set()
        for edge in self._edges:
            out.add(edge.to_screen)
            if edge.from_screen:
                out.add(edge.from_screen)
        return out

    @classmethod
    def from_operations(
        cls,
        operations: Mapping[str, dict[str, Any]],
        *,
        executor_effects: Mapping[str, Any] | None = None,
    ) -> "OperationGraph":
        edges: list[NavEdge] = []
        verifiers: list[NavEdge] = []
        for name, operation in sorted(operations.items()):
            if str(operation.get("status") or "") != "active":
                continue
            nav = operation.get("navigation")
            if not isinstance(nav, dict) or not nav.get("to"):
                continue
            from_screen = str(nav["from"]) if nav.get("from") else None
            to_screen = str(nav["to"])
            checkpoint_screens = {
                str(value)
                for value in dict(operation.get("screen_expectations") or {}).values()
                if str(value)
            }
            safety, effects, findings = admission_mod.operation_safety(
                operation,
                operations=operations,
                executor_effects=executor_effects,
            )
            edge = NavEdge(
                operation=str(name),
                revision=int(operation.get("revision") or 0),
                from_screen=from_screen,
                to_screen=to_screen,
                safety=safety,
                effects=tuple(effects),
                requires=dict(nav.get("requires") or {}),
                sensitive=tuple(findings),
            )
            if from_screen == to_screen:
                raw_steps = operation.get("steps") or []
                first_step = raw_steps[0] if raw_steps and isinstance(raw_steps[0], dict) else {}
                if (
                    to_screen in checkpoint_screens
                    and nav.get("reset")
                    and str(first_step.get("operation") or "") in START_RESET_OPERATIONS
                    and safety == admission_mod.READ_ONLY
                    and not effects
                    and not findings
                ):
                    verifiers.append(edge)
                continue  # 自环只可作起始 Screen 验证器，不进入普通 BFS
            if to_screen not in checkpoint_screens:
                continue  # 没有目标 Screen checkpoint 的动作不能作为可证明导航边
            edges.append(edge)
        return cls(edges, verifiers=verifiers)

    def _candidates(self, node: str) -> list[NavEdge]:
        return list(self._out.get(node, [])) + list(self._out.get(None, []))

    def route(
        self,
        target: str,
        *,
        start: str = DEFAULT_START_SCREEN,
        context: Mapping[str, Any] | None = None,
        include_mutating: bool = False,
    ) -> tuple[list[NavEdge], str | None]:
        """BFS 最短路径。返回 ``(edges, reason)``；找不到时 edges 为空、reason 说明原因。

        ``include_mutating=False``（默认）时只走只读边——无人值守的影响验证不该顺手改
        购物车或订单状态。需要带 effect 的路径时由调用方显式打开，并且仍要过 admission。
        """
        if target == start:
            blocked_by_context: list[str] = []
            for verifier in self._verifiers:
                if verifier.to_screen != target:
                    continue
                ok, missing = ctx_mod.context_satisfies(context, verifier.requires)
                if ok:
                    return [verifier], None
                blocked_by_context.extend(missing)
            if blocked_by_context:
                return [], f"起始 Screen 验证器上下文不满足：{sorted(set(blocked_by_context))}"
            return [], None
        seen = {start}
        queue: deque[tuple[str, list[NavEdge]]] = deque([(start, [])])
        blocked_by_context: list[str] = []
        blocked_by_effect = False
        while queue:
            node, path = queue.popleft()
            for edge in self._candidates(node):
                if edge.to_screen in seen:
                    continue
                if edge.safety == admission_mod.MUTATING and not include_mutating:
                    blocked_by_effect = True
                    continue
                ok, missing = ctx_mod.context_satisfies(context, edge.requires)
                if not ok:
                    blocked_by_context.extend(missing)
                    continue
                next_path = path + [edge]
                if edge.to_screen == target:
                    return next_path, None
                seen.add(edge.to_screen)
                queue.append((edge.to_screen, next_path))
        if blocked_by_context:
            return [], f"上下文不满足：{sorted(set(blocked_by_context))}"
        if blocked_by_effect:
            return [], "仅存在带副作用的路径，只读探索不予通行"
        return [], f"没有从 {start} 到 {target} 的已注册 active 只读路径"


def _target_confidence(match: bindings_mod.BindingMatch) -> str:
    return match.confidence


def plan(
    *,
    app: str,
    build: str,
    ingested: manifest_mod.IngestedManifest,
    bindings_doc: dict[str, Any],
    graph: OperationGraph,
    allowed_effects: Sequence[str] = (),
    consumer_context: Mapping[str, Any] | None = None,
    known_screens: Sequence[str] | None = None,
    start_screen: str = DEFAULT_START_SCREEN,
    statuses: Sequence[str] | None = ("active",),
) -> dict[str, Any]:
    """变更 → impact-plan.v1 文档（纯函数，不碰设备）。"""
    targets: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    requested_effects: list[str] = []
    seen_targets: dict[str, dict[str, Any]] = {}
    known = {str(s) for s in (known_screens or [])}

    for change in ingested.changes:
        if change.quality == manifest_mod.QUALITY_UNUSABLE:
            gaps.append({
                "kind": "unusable_input",
                "detail": (
                    f"变更 {change.label} 既没有可用的 data/config key，也没有可解析的 endpoint："
                    "无法自动推断影响面，请补 change manifest（§10 最后一档）"
                ),
            })
            continue
        matches = bindings_mod.resolve(
            bindings_doc,
            data_keys=change.data_keys,
            endpoint_keys=change.endpoint_keys,
            response_paths=change.response_paths,
            current_build=build,
            statuses=statuses,
        )
        if not matches:
            kind = "endpoint_only_no_binding" if change.endpoint_keys else "no_binding"
            gaps.append({
                "kind": kind,
                "detail": (
                    f"变更 {change.label} 没有命中任何 FieldBinding；"
                    "需要人工 catalog 补一条绑定，或先跑一次观测把该接口纳入图谱"
                ),
                **({"data_key": change.data_keys[0]} if change.data_keys else {}),
                **({"endpoint_key": change.endpoint_keys[0]} if change.endpoint_keys else {}),
            })
            continue
        for match in matches:
            binding = match.binding
            binding_id = str(binding.get("id"))
            place = bindings_mod.targets_for(match)
            dedupe_key = f"{binding_id}|{place['screen_id']}|{ctx_mod.variant_key(place['variant_selector'])}"
            existing = seen_targets.get(dedupe_key)
            if existing is not None:
                _merge_change_into_target(existing, change)
                continue
            target = _build_target(
                target_id=f"T{len(targets) + 1}",
                binding=binding,
                match=match,
                place=place,
                change=change,
                graph=graph,
                consumer_context=consumer_context,
                allowed_effects=allowed_effects,
                known_screens=known,
                start_screen=start_screen,
            )
            requested_effects.extend(target.get("route", {}).get("effects") or [])
            targets.append(target)
            seen_targets[dedupe_key] = target
            if target.get("blocked_reason") == "route_unavailable":
                gaps.append({
                    "kind": "route_unavailable",
                    "detail": (
                        f"{binding_id} 指向 {place['screen_id']}，但"
                        f"{target['route'].get('reason') or '没有可用路径'}"
                    ),
                })
            elif target.get("blocked_reason") == "stale_binding":
                gaps.append({
                    "kind": "stale_binding",
                    "detail": f"{binding_id} 的证据已失效（build 或连续 no-match），需重新标定",
                })
            elif target.get("blocked_reason") == "unknown_screen":
                gaps.append({
                    "kind": "unknown_screen",
                    "detail": f"{binding_id} 指向的 screen={place['screen_id']} 不在 build={build} 的资产里",
                })

    decision = admission_mod.decide(allowed_effects, requested_effects)
    doc = {
        "schema_version": "v1",
        "app": str(app),
        "build": str(build),
        "change_id": ingested.change_id,
        "env": ingested.env,
        "generated_at": now_iso(),
        "input_quality": ingested.quality,
        "targets": targets,
        "route_groups": compress_routes(targets),
        "gaps": gaps,
        "admission": decision.as_dict(),
    }
    validate_or_raise("impact-plan.v1.json", doc)
    return doc


def _merge_change_into_target(target: dict[str, Any], change: manifest_mod.IngestedChange) -> None:
    keys = list(target.get("data_keys") or [])
    for key in change.data_keys:
        if key not in keys:
            keys.append(key)
    if keys:
        target["data_keys"] = keys
    paths = list(target.get("response_paths") or [])
    for path in change.response_paths:
        if path not in paths:
            paths.append(path)
    if paths:
        target["response_paths"] = paths


def _build_target(
    *,
    target_id: str,
    binding: dict[str, Any],
    match: bindings_mod.BindingMatch,
    place: dict[str, Any],
    change: manifest_mod.IngestedChange,
    graph: OperationGraph,
    consumer_context: Mapping[str, Any] | None,
    allowed_effects: Sequence[str],
    known_screens: set[str],
    start_screen: str,
) -> dict[str, Any]:
    confidence = _target_confidence(match)
    target: dict[str, Any] = {
        "target_id": target_id,
        "binding_id": str(binding.get("id")),
        "screen_id": place["screen_id"],
        "confidence": confidence,
        "execution": EXECUTION_BY_CONFIDENCE.get(confidence, "candidate"),
    }
    if "comparison_policy" in binding:
        target["comparison_policy"] = deepcopy(binding["comparison_policy"])
        target["binding_revision"] = binding["revision"]
        target["binding_sha256"] = policy_mod.binding_digest(binding)
    if place["variant_selector"]:
        target["variant_selector"] = place["variant_selector"]
    if place["regions"]:
        target["regions"] = place["regions"]
    if change.data_keys:
        target["data_keys"] = list(change.data_keys)
    endpoint_key = (
        change.endpoint_keys[0]
        if change.endpoint_keys
        else endpoint_mod.endpoint_key_from_binding(binding.get("endpoint"))
    )
    if endpoint_key:
        target["endpoint_key"] = endpoint_key
    response_paths = list(change.response_paths)
    if not response_paths and binding.get("response_path"):
        response_paths = [
            endpoint_mod.normalize_response_path(str(binding["response_path"]))
        ]
    if response_paths:
        target["response_paths"] = response_paths
    required = dict(place["variant_selector"])
    if required:
        target["required_context"] = required

    if confidence == bindings_mod.STALE:
        target["execution"] = "blocked"
        target["blocked_reason"] = "stale_binding"
        target["route"] = {"status": "unavailable", "reason": "证据已失效，先重新标定再谈路径"}
        return target

    if known_screens and place["screen_id"] not in known_screens:
        target["execution"] = "blocked"
        target["blocked_reason"] = "unknown_screen"
        target["route"] = {"status": "unavailable", "reason": "目标 screen 不在当前 build 的资产里"}
        return target

    edges, reason = graph.route(
        place["screen_id"], start=start_screen, context=consumer_context
    )
    if reason is not None:
        # 只读走不通时，再算一次含副作用的路径。**不是为了走它**，而是为了把
        # 「压根没有路」和「有路但需要未获准的 effect」分开报——两者的下一步动作
        # 完全不同：前者要建 operation，后者要走 Effect Admission 登记。
        mutating_edges, mutating_reason = graph.route(
            place["screen_id"], start=start_screen,
            context=consumer_context, include_mutating=True,
        )
        if mutating_reason is not None:
            target["execution"] = "blocked"
            target["blocked_reason"] = "route_unavailable"
            target["route"] = {"status": "unavailable", "reason": reason}
            return target
        edges = mutating_edges

    steps = [edge.as_step() for edge in edges]
    effects = admission_mod.route_effects(steps)
    sensitive = [item for edge in edges for item in edge.sensitive]
    route: dict[str, Any] = {
        "status": "ready",
        "steps": steps,
        "effects": effects,
        "safety": admission_mod.MUTATING if (effects or sensitive) else admission_mod.READ_ONLY,
    }
    target["route"] = route

    if sensitive:
        target["execution"] = "blocked"
        target["blocked_reason"] = admission_mod.finding_blocker(sensitive)
        if target["blocked_reason"] == "effect_declaration_missing":
            route["reason"] = (
                "路径中的 Operation 引用缺少可审计 effect 声明："
                + "、".join(sorted({str(f["marker"]) for f in sensitive}))
            )
        else:
            route["reason"] = (
                "路径上出现敏感 CTA："
                + "、".join(sorted({str(f["marker"]) for f in sensitive}))
            )
        return target
    if effects:
        decision = admission_mod.decide(allowed_effects, effects)
        if not decision.ok:
            target["execution"] = "blocked"
            target["blocked_reason"] = "effect_not_admitted"
            route["reason"] = decision.note
    return target


def compress_routes(targets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """只读共享前缀合并（§11.2）。

    能合的只有「都可执行、都只读、上下文不冲突」的目标；带 effect、被拒、上下文互斥或
    路径不可用的目标各自独立成组，并写明拆分原因——合错组会让一次 run 里的副作用污染
    另一个目标的证据。
    """
    groups: list[dict[str, Any]] = []
    mergeable: list[dict[str, Any]] = []
    for target in targets:
        route = dict(target.get("route") or {})
        if (
            route.get("status") != "ready"
            or target.get("execution") == "blocked"
            or route.get("safety") != admission_mod.READ_ONLY
        ):
            groups.append({
                "group_id": f"G{len(groups) + 1}",
                "target_ids": [target["target_id"]],
                "split_reason": _split_reason(target),
            })
            continue
        mergeable.append(target)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for target in mergeable:
        key = ctx_mod.variant_key(target.get("required_context"))
        buckets.setdefault(key, []).append(target)
    for key in sorted(buckets):
        members = buckets[key]
        paths = [
            [str(step.get("operation")) for step in (t.get("route") or {}).get("steps") or []]
            for t in members
        ]
        groups.append({
            "group_id": f"G{len(groups) + 1}",
            "target_ids": [t["target_id"] for t in members],
            "shared_prefix": _common_prefix(paths),
        })
    return groups


def _split_reason(target: dict[str, Any]) -> str:
    reason = str(target.get("blocked_reason") or "")
    if reason:
        return reason
    route = dict(target.get("route") or {})
    if route.get("status") != "ready":
        return "route_unavailable"
    if route.get("safety") != admission_mod.READ_ONLY:
        return "mutating_route"
    return "not_mergeable"


def _common_prefix(paths: Sequence[Sequence[str]]) -> list[str]:
    if not paths:
        return []
    prefix: list[str] = []
    for items in zip(*paths):
        first = items[0]
        if all(item == first for item in items):
            prefix.append(first)
        else:
            break
    return prefix
