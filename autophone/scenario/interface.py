"""Network Scenario intent interface, separate from read-only Observation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .controller import LaunchPlan, ScenarioController, ScenarioStartError


class PreparedScenario:
    """Freeze a plan and own its write session; backend handles stay private.

    The caller supplies the execution lease and serializes this lifecycle on its
    owner thread. Preparation performs no device writes.
    """

    def __init__(self, controller: ScenarioController, scenario: dict[str, Any],
                 plan: LaunchPlan, *, device: str) -> None:
        self.__controller = controller
        self.__scenario = deepcopy(scenario)
        self.__plan = deepcopy(plan)
        self.__device = device
        self.__session = None
        self.__attempted = False
        self.__closed = None

    @property
    def plan(self) -> LaunchPlan:
        return deepcopy(self.__plan)

    def start(self, *, session_id: str | None = None) -> dict[str, Any]:
        if self.__closed is not None or self.__attempted:
            raise ScenarioStartError("PreparedScenario start can be consumed only once")
        self.__attempted = True
        self.__session = self.__controller.start(
            deepcopy(self.__scenario), self.__plan,
            device=self.__device, session_id=session_id,
        )
        return {"session_id": getattr(self.__session, "session_id", session_id),
                "app": self.__plan.app, "device": self.__device,
                "rule_snapshot_hash": self.__plan.rule_snapshot_hash}

    def status(self) -> dict[str, Any]:
        if self.__closed is not None:
            return deepcopy(self.__closed)
        if self.__session is None:
            raise ScenarioStartError("PreparedScenario has not started")
        return deepcopy(self.__controller.status(self.__session))

    def close(self) -> dict[str, Any]:
        if self.__closed is None:
            if self.__session is None:
                raise ScenarioStartError("PreparedScenario has no session to close")
            self.__closed = deepcopy(self.__controller.close(self.__session))
        return deepcopy(self.__closed)


def prepare(scenario_spec: dict[str, Any], *, controller: ScenarioController,
            mode: str, surface_policy: str = "recover_home",
            device: str = "vphone-0") -> PreparedScenario:
    frozen = deepcopy(scenario_spec)
    plan = controller.preflight(frozen, mode=mode, surface_policy=surface_policy)
    return PreparedScenario(controller, frozen, plan, device=device)
