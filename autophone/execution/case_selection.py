from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_STATUSES = ("active",)
DEFAULT_AUTOMATION = "ui_ready"


@dataclass(frozen=True)
class CaseSelector:
    ids: tuple[str, ...] = field(default_factory=tuple)
    tag: str | None = None
    module: str | None = None
    suite: str | None = None
    flow: str | None = None
    business_family: str | None = None
    family_id: str | None = None
    pair_id: str | None = None
    statuses: tuple[str, ...] = DEFAULT_STATUSES

    @classmethod
    def from_raw(
        cls,
        raw: "CaseSelector | dict[str, Any] | None" = None,
        *,
        automation: str | None = None,
        ids: list[str] | tuple[str, ...] | None = None,
    ) -> "CaseSelector":
        if isinstance(raw, CaseSelector):
            base: dict[str, Any] = raw.to_dict()
        else:
            base = dict(raw or {})
        if automation is not None and "tag" not in base:
            base["tag"] = automation
        if ids is not None and "ids" not in base:
            base["ids"] = list(ids)
        plain_default = not base
        statuses = base.get("statuses")
        if statuses is None:
            statuses = list(DEFAULT_STATUSES)
        elif isinstance(statuses, str):
            statuses = [statuses]
        raw_ids = base.get("ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        # A plain regression run is deliberately the safe lane: only active
        # cases that have passed the ui_ready admission contract.  Stateful
        # fixture work must be named explicitly by id, suite, flow, or tag;
        # otherwise an innocent "run all" can consume an order.
        tag = _str_or_none(base.get("tag"))
        if plain_default and tag is None:
            tag = DEFAULT_AUTOMATION
        return cls(
            ids=tuple(str(i) for i in raw_ids if str(i)),
            tag=tag,
            module=_str_or_none(base.get("module")),
            suite=_str_or_none(base.get("suite")),
            flow=_str_or_none(base.get("flow")),
            business_family=_str_or_none(base.get("business_family")),
            family_id=_str_or_none(base.get("family_id")),
            pair_id=_str_or_none(base.get("pair_id")),
            statuses=tuple(str(s) for s in statuses if str(s)),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"statuses": list(self.statuses or DEFAULT_STATUSES)}
        if self.ids:
            out["ids"] = list(self.ids)
        if self.tag:
            out["tag"] = self.tag
        if self.module:
            out["module"] = self.module
        if self.suite:
            out["suite"] = self.suite
        if self.flow:
            out["flow"] = self.flow
        if self.business_family:
            out["business_family"] = self.business_family
        if self.family_id:
            out["family_id"] = self.family_id
        if self.pair_id:
            out["pair_id"] = self.pair_id
        return out


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _flow_spec(case: dict[str, Any], flow_id: str) -> dict[str, Any] | None:
    """Return one declared flow membership, supporting a second flow per case.

    A lifecycle operation such as R132 can be shared by an existing-order flow
    and a fixture-contract flow.  Keeping that multiplicity inside selection
    prevents callers from copying the same operation into a second test case.
    """
    raw = [case.get("flow"), *(case.get("flows") or [])]
    matches = [spec for spec in raw if isinstance(spec, dict) and spec.get("id") == flow_id]
    if len(matches) > 1:
        raise ValueError(f"case {case.get('id')}: flow {flow_id!r} is declared more than once")
    return matches[0] if matches else None


def _flow_ids(case: dict[str, Any]) -> set[str]:
    raw = [case.get("flow"), *(case.get("flows") or [])]
    return {str(spec.get("id")) for spec in raw if isinstance(spec, dict) and spec.get("id")}


def order_flow_cases(cases: list[dict[str, Any]], flow_id: str) -> list[dict[str, Any]]:
    """Validate and order one complete lifecycle flow."""
    matched: list[tuple[int, dict[str, Any]]] = []
    for case in cases:
        flow = _flow_spec(case, flow_id)
        if flow is None:
            continue
        step = flow.get("step")
        if not isinstance(step, int) or step <= 0:
            raise ValueError(f"case {case.get('id')}: flow {flow_id!r} needs positive integer step")
        matched.append((step, case))
    steps = [step for step, _case in matched]
    if len(steps) != len(set(steps)):
        raise ValueError(f"flow {flow_id!r} has duplicate step numbers")
    ordered = sorted(matched, key=lambda pair: pair[0])
    for (_prev_step, prev), (_next_step, current) in zip(ordered, ordered[1:]):
        prev_flow = _flow_spec(prev, flow_id) or {}
        current_flow = _flow_spec(current, flow_id) or {}
        if prev_flow.get("produces") != current_flow.get("requires"):
            raise ValueError(
                f"flow {flow_id!r}: {prev.get('id')} produces "
                f"{prev_flow.get('produces')!r}, but {current.get('id')} requires "
                f"{current_flow.get('requires')!r}")
    return [case for _step, case in ordered]


def canonical_selector(raw: CaseSelector | dict[str, Any] | None) -> dict[str, Any]:
    selector = CaseSelector.from_raw(raw)
    out: dict[str, Any] = {"statuses": sorted(set(selector.statuses or DEFAULT_STATUSES))}
    if selector.ids:
        out["ids"] = sorted(set(selector.ids))
    if selector.tag:
        out["tag"] = selector.tag
    if selector.module:
        out["module"] = selector.module
    if selector.suite:
        out["suite"] = selector.suite
    if selector.flow:
        out["flow"] = selector.flow
    if selector.business_family:
        out["business_family"] = selector.business_family
    if selector.family_id:
        out["family_id"] = selector.family_id
    if selector.pair_id:
        out["pair_id"] = selector.pair_id
    return out


def selector_key(raw: CaseSelector | dict[str, Any] | None) -> str:
    return json.dumps(canonical_selector(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def select_cases(cases: list[dict[str, Any]], raw: CaseSelector | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    selector = CaseSelector.from_raw(raw)
    if selector.flow and (
        selector.ids or selector.tag or selector.module or selector.suite
        or selector.business_family or selector.family_id or selector.pair_id
    ):
        raise ValueError(
            "flow selection cannot be combined with ids/tag/module/suite/family; "
            "a flow must execute its complete declared state sequence")
    out = cases
    if selector.statuses:
        statuses = set(selector.statuses)
        # 缺失生命周期的未迁移条目不能被推断为 active，避免进入执行选择面。
        out = [c for c in out if str(c.get("status") or "") in statuses]
    if selector.tag:
        out = [c for c in out if c.get("automation") == selector.tag]
    if selector.module:
        out = [c for c in out if c.get("module") == selector.module]
    if selector.business_family:
        out = [c for c in out if c.get("business_family") == selector.business_family]
    if selector.family_id:
        out = [c for c in out if c.get("family_id") == selector.family_id]
    if selector.pair_id:
        out = [c for c in out if c.get("pair_id") == selector.pair_id]
    if selector.suite:
        out = [c for c in out if selector.suite in [str(s) for s in (c.get("suites") or [])]]
        # A suite can be a named lifecycle flow.  When every selected member
        # advertises the same flow, preserve its state order even though the
        # source YAML is grouped by original spreadsheet row.
        shared_flow_ids = set.intersection(*(_flow_ids(case) for case in out)) if out else set()
        if len(shared_flow_ids) == 1:
            out = order_flow_cases(out, next(iter(shared_flow_ids)))
    if selector.flow:
        # A lifecycle flow is deliberately explicit per case.  Ordinary suite
        # selection preserves authored order; a flow is the exception because
        # its order encodes state dependencies (arrival -> pickup -> delivery
        # start -> completion).  Reject malformed/ambiguous numbering rather
        # than silently falling back to YAML order.
        out = order_flow_cases(out, selector.flow)
    if selector.ids:
        idset = set(selector.ids)
        out = [c for c in out if c.get("id") in idset]
    return out
