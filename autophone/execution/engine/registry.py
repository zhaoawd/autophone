"""registry.py — the function-style step-script registry (docs §10.2).

Decision recorded in the design: step-scripts are hand-written executable Python,
not a YAML DSL. Each script is `Callable[[CaseContext], None]` registered by case
id. The runner looks up CASE_SCRIPTS[case.id] and calls it with a CaseContext.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from autophone.execution.engine.actions import CaseContext

StepScript = Callable[["CaseContext"], None]

CASE_SCRIPTS: dict[str, StepScript] = {}


def case(case_id: str) -> Callable[[StepScript], StepScript]:
    """Register a step-script under `case_id` (e.g. @case("R22"))."""
    def deco(fn: StepScript) -> StepScript:
        if case_id in CASE_SCRIPTS:
            raise ValueError(f"duplicate step-script for {case_id}")
        CASE_SCRIPTS[case_id] = fn
        return fn
    return deco
