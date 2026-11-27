"""Runtime registration of App-owned execution planning policies."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ExecutionPlanner:
    build_current_plan: Callable[[list[dict[str, Any]]], Any]
    build_selected_groups: Callable[[list[dict[str, Any]]], Any]


_PLANNERS: dict[str, ExecutionPlanner] = {}


def register_execution_planner(app: str, planner: ExecutionPlanner) -> None:
    _PLANNERS[app] = planner


def execution_planner(app: str) -> ExecutionPlanner:
    """Load the selected App adapter on demand and require explicit registration."""
    if app not in _PLANNERS:
        importlib.import_module(f"apps.{app}.execution_plan")
    try:
        return _PLANNERS[app]
    except KeyError as exc:
        raise ValueError(f"App did not register an execution planner: {app}") from exc


def has_execution_planner(app: str) -> bool:
    """Return whether ``apps.<app>.execution_plan`` exists and registers a planner."""
    if not app:
        return False
    if app in _PLANNERS:
        return True
    try:
        importlib.import_module(f"apps.{app}.execution_plan")
    except ModuleNotFoundError as exc:
        if exc.name in {"apps", f"apps.{app}", f"apps.{app}.execution_plan"}:
            return False
        raise
    return app in _PLANNERS
