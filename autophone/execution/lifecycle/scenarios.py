"""Lifecycle scenario definitions for order cases.

Models order cases as parameterized state-machine paths instead of one Python
function per Excel row. Leaves GPS-distance variants explicitly blocked until
controllable device location is configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from autophone.execution.fixtures.contract import FixtureContext


class ScenarioUnsupported(ValueError):
    """Raised when a case is known but cannot run under current fixture facts."""


@dataclass(frozen=True)
class DialogSpec:
    query: str
    tap_query: str


@dataclass(frozen=True)
class LifecycleStep:
    name: str
    action: str
    text: str | None = None
    verify_state: str | None = None
    dialog: DialogSpec | None = None
    note: str = ""


@dataclass(frozen=True)
class LifecycleScenario:
    scenario_id: str
    name: str
    order_type: str
    case_ids: tuple[str, ...]
    distance_band: str = "unknown"
    entry_text: str = "当前订单"
    steps: tuple[LifecycleStep, ...] = field(default_factory=tuple)
    state_assertions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    case_state_map: dict[str, tuple[str, ...]] = field(default_factory=dict)


RESIDENT_NORMAL_MAIN = LifecycleScenario(
    scenario_id="resident_normal_main",
    name="驻店普通单主路径：取餐 -> 开始送餐 -> 送达 -> 返店",
    order_type="resident_normal",
    case_ids=("R29", "R30", "R32", "R35"),
    steps=(
        LifecycleStep(
            name="tap_pickup",
            action="tap_text",
            text="取餐",
            dialog=DialogSpec(query="检查商品", tap_query="确定"),
            verify_state="picked_up",
        ),
        LifecycleStep(
            name="tap_start_delivery",
            action="tap_text",
            text="开始送餐",
            dialog=DialogSpec(query="位置距离店铺超过200米", tap_query="确认离店"),
            verify_state="delivery_started",
        ),
        LifecycleStep(
            name="tap_delivered",
            action="tap_text",
            text="确认送达",
            dialog=DialogSpec(query="是否完成送达", tap_query="确定"),
            verify_state="delivered",
        ),
        LifecycleStep(
            name="tap_return_store",
            action="tap_text",
            text="确认到店",
            dialog=DialogSpec(query="确认操作到店", tap_query="确认"),
            verify_state="returned_to_store",
        ),
    ),
    state_assertions={
        "picked_up": ("已取餐",),
        "delivery_started": ("当前任务",),
        "delivered": ("订单已完成",),
        "returned_to_store": ("到店成功",),
    },
    case_state_map={
        "R29": ("picked_up",),
        "R30": ("delivery_started",),
        "R32": ("delivered",),
        "R35": ("returned_to_store",),
    },
)

SCENARIOS: dict[str, LifecycleScenario] = {
    RESIDENT_NORMAL_MAIN.scenario_id: RESIDENT_NORMAL_MAIN,
}

CASE_TO_SCENARIO: dict[str, str] = {
    case_id: RESIDENT_NORMAL_MAIN.scenario_id
    for case_id in RESIDENT_NORMAL_MAIN.case_ids
}

GPS_BLOCKED_CASES = {
    "R31": "store_distance_over_200m",
    "R33": "customer_distance_over_200m",
    "R36": "return_store_distance_over_200m",
}


def scenario_for_case(case_id: str, fx: FixtureContext | None = None) -> LifecycleScenario:
    if case_id in GPS_BLOCKED_CASES:
        raise ScenarioUnsupported(
            f"{case_id} requires actor/customer distance control "
            f"({GPS_BLOCKED_CASES[case_id]}), but location control is unresolved"
        )
    scenario_id = CASE_TO_SCENARIO.get(case_id)
    if scenario_id is None:
        raise ScenarioUnsupported(f"no lifecycle scenario mapped for case {case_id}")
    scenario = SCENARIOS[scenario_id]
    if fx is not None:
        order_type = str(fx.order.get("type") or "")
        if order_type and order_type != scenario.order_type:
            raise ScenarioUnsupported(
                f"case {case_id} maps to {scenario.order_type}, fixture order type is {order_type}"
            )
    return scenario
