"""Stable case scheduling contract.

``raw.hints`` is conversion/advisory material and must never change runtime
behaviour.  This module is the one interface through which the batch runner
learns which setup prefix may be shared, which operations are coupled to API
attribution, and how a pooled order may be released.

Absent ``case.scheduling`` means fully conservative behaviour.  Present values
are validated against ``scheduling.v1.json`` plus the case's actual setup stack;
invalid declarations fail loud instead of silently changing case semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autophone.contracts import ContractError, validate_or_raise


class SchedulingContractError(ValueError):
    """A case scheduling declaration is malformed or contradicts its setup."""


@dataclass(frozen=True)
class OrderPoolContract:
    mode: str
    produce: str | None = None


@dataclass(frozen=True)
class SchedulingContract:
    shareable_prefix: tuple[str, ...] = ()
    expect_coupled_operations: frozenset[str] = frozenset()
    order_pool: OrderPoolContract | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.shareable_prefix:
            out["shareable_prefix"] = list(self.shareable_prefix)
        if self.expect_coupled_operations:
            out["expect_coupled_operations"] = sorted(self.expect_coupled_operations)
        if self.order_pool is not None:
            order = {"mode": self.order_pool.mode}
            if self.order_pool.produce is not None:
                order["produce"] = self.order_pool.produce
            out["order_pool"] = order
        return out


EMPTY_SCHEDULING = SchedulingContract()


def setup_refs(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the declared Operation refs used by scheduling analysis and Runner."""
    return [
        ref
        for ref in (case.get("setup") or [])
        if isinstance(ref, dict) and ref.get("op")
    ]


def _setup_operations(case: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for index, ref in enumerate(case.get("setup") or []):
        if not isinstance(ref, dict) or not isinstance(ref.get("op"), str) or not ref["op"].strip():
            raise SchedulingContractError(
                f"case {case.get('id', '?')} setup[{index}] must be an operation ref with non-empty op"
            )
        out.append(ref["op"].strip())
    return out


def contract_for_case(case: dict[str, Any]) -> SchedulingContract:
    """Validate and return the stable scheduling declaration for one case.

    The interface deliberately accepts the whole case so semantic checks stay
    local: a declared prefix must equal the actual leading setup operations and
    an API-coupled operation cannot simultaneously be declared shareable.
    """
    raw = case.get("scheduling")
    if raw is None:
        return EMPTY_SCHEDULING
    if not isinstance(raw, dict):
        raise SchedulingContractError(
            f"case {case.get('id', '?')} scheduling must be an object"
        )
    try:
        validate_or_raise("scheduling.v1.json", raw)
    except ContractError as exc:
        raise SchedulingContractError(f"case {case.get('id', '?')}: {exc}") from exc

    prefix = tuple(str(op).strip() for op in raw.get("shareable_prefix") or [])
    coupled_values = tuple(
        str(op).strip() for op in raw.get("expect_coupled_operations") or []
    )
    if any(not op for op in (*prefix, *coupled_values)):
        raise SchedulingContractError(
            f"case {case.get('id', '?')} scheduling operation names must not be blank"
        )
    if len(set(prefix)) != len(prefix) or len(set(coupled_values)) != len(coupled_values):
        raise SchedulingContractError(
            f"case {case.get('id', '?')} scheduling operation names must be unique after trimming"
        )
    coupled = frozenset(coupled_values)
    if prefix:
        actual = _setup_operations(case)
        if actual[:len(prefix)] != list(prefix):
            raise SchedulingContractError(
                f"case {case.get('id', '?')} scheduling.shareable_prefix {list(prefix)!r} "
                f"does not match setup prefix {actual[:len(prefix)]!r}"
            )
        conflict = sorted(set(prefix) & coupled)
        if conflict:
            raise SchedulingContractError(
                f"case {case.get('id', '?')} operation(s) cannot be both shareable and "
                f"expect-coupled: {conflict}"
            )

    order_raw = raw.get("order_pool")
    order = None
    if isinstance(order_raw, dict):
        order = OrderPoolContract(
            mode=str(order_raw["mode"]),
            produce=str(order_raw["produce"]).strip() if "produce" in order_raw else None,
        )
        if order.produce is not None and not order.produce:
            raise SchedulingContractError(
                f"case {case.get('id', '?')} scheduling.order_pool.produce must not be blank"
            )
        setup_ops = _setup_operations(case)
        if setup_ops.count("create_order") != 1:
            raise SchedulingContractError(
                f"case {case.get('id', '?')} scheduling.order_pool requires exactly one "
                f"create_order setup operation, found {setup_ops.count('create_order')}"
            )
        consumes = case.get("order_consumes")
        if consumes is True and order.mode in {"read_only", "advance"}:
            raise SchedulingContractError(
                f"case {case.get('id', '?')} order_consumes=true conflicts with "
                f"scheduling.order_pool.mode={order.mode}"
            )
        if consumes is False and order.mode == "advance":
            raise SchedulingContractError(
                f"case {case.get('id', '?')} order_consumes=false conflicts with "
                "scheduling.order_pool.mode=advance"
            )
    return SchedulingContract(
        shareable_prefix=prefix,
        expect_coupled_operations=coupled,
        order_pool=order,
    )


def validate_scheduling(value: Any, *, case: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate an authoring value and return its canonical persisted shape."""
    probe = dict(case or {})
    probe["scheduling"] = value
    return contract_for_case(probe).to_dict()
