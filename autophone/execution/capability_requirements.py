"""Single-source derivation and planning for regression capabilities."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from autophone.assets.entity_binding import (
    case_requires_api_capture,
    case_requires_entity_readback,
)
from autophone.paths import CODE_ROOT
from autophone.execution.contracts import blocker
from autophone.execution.engine.recipe import operation_requirements_for_app


MODES = frozenset({"required", "advisory", "disabled"})
_RANK = {"disabled": 0, "advisory": 1, "required": 2}
_SAFETY_REQUIRED = frozenset({"vm_uds", "app_ui", "location_injection"})
_CAPABILITY_PRIORITY = {"vm_uds": 0, "app_ui": 1, "location_injection": 2}


def _merge(requirements: dict[str, str], explicit: Mapping[str, Any]) -> None:
    for capability, raw_mode in explicit.items():
        mode = str(raw_mode)
        if mode not in MODES:
            raise ValueError(f"invalid requirement mode for {capability}: {mode}")
        current = requirements.get(str(capability), "disabled")
        if capability in _SAFETY_REQUIRED and current == "required" and mode != "required":
            raise ValueError(f"requirements cannot weaken required safety capability {capability}")
        if _RANK[mode] > _RANK[current]:
            requirements[str(capability)] = mode


def _default_api_capture_mode(
    profile: Any,
    profile_requirements: Mapping[str, Any],
    *,
    api_plane_mode: str | None = None,
) -> str:
    """Resolve the starting ``api_capture`` requirement mode before monotonic merges."""
    if api_plane_mode == "disabled":
        return "disabled"
    if api_plane_mode == "required":
        return "required"
    if api_plane_mode in {"advisory", "controlled"}:
        return "advisory"
    raw_caps = (getattr(profile, "raw", {}) or {}).get("capabilities") or {}
    api_cap = raw_caps.get("api_capture") if isinstance(raw_caps, Mapping) else None
    if isinstance(api_cap, Mapping) and api_cap.get("enabled") is False:
        return "disabled"
    if str(profile_requirements.get("api_capture") or "") == "disabled":
        return "disabled"
    return "advisory"


def derive_requirements(
    profile: Any,
    case: Mapping[str, Any],
    recipe_document: Mapping[str, Any],
    *,
    api_plane_mode: str | None = None,
) -> dict[str, str]:
    """Derive defaults first, then apply profile and recipe declarations monotonically."""
    profile_requirements = getattr(profile, "requirements", {}) or {}
    if not isinstance(profile_requirements, Mapping):
        raise ValueError("profile requirements must be a mapping")
    recipe_requirements = recipe_document.get("requirements") or {}
    if not isinstance(recipe_requirements, Mapping):
        raise ValueError("recipe requirements must be a mapping")
    default_api_mode = _default_api_capture_mode(
        profile,
        profile_requirements,
        api_plane_mode=api_plane_mode,
    )
    requirements = {"vm_uds": "required", "app_ui": "required", "api_capture": default_api_mode}
    if str(case.get("login_precondition") or "required") == "required":
        requirements["authenticated_session"] = "required"
    if case_requires_api_capture(case):
        requirements["api_capture"] = "required"
    _merge(requirements, profile_requirements)
    _merge(requirements, recipe_requirements)
    steps = recipe_document.get("steps") or []
    if not isinstance(steps, list):
        raise ValueError("recipe steps must be a list")
    profile_root = getattr(profile, "root", None) or CODE_ROOT
    for step in steps:
        if not isinstance(step, Mapping) or "operation" not in step:
            continue
        for capability in operation_requirements_for_app(
            profile.app,
            str(step["operation"]),
            root=profile_root,
        ):
            requirements[capability] = "required"
    if case_requires_entity_readback(case):
        binding = case.get("entity_binding")
        readback = binding.get("readback") if isinstance(binding, Mapping) else None
        readback_op = readback.get("op") if isinstance(readback, Mapping) else None
        if isinstance(readback_op, str) and readback_op.strip():
            for capability in operation_requirements_for_app(
                profile.app,
                readback_op.strip(),
                root=profile_root,
            ):
                requirements[capability] = "required"
    return requirements


def plan_cases(cases: Iterable[Mapping[str, Any]], availability: Mapping[str, bool]) -> list[dict[str, Any]]:
    """Keep case order while separating blocked required needs from advisory debt."""
    plan: list[dict[str, Any]] = []
    boundary_before_next = False
    for raw_case in cases:
        case = dict(raw_case)
        requirements = case.get("requirements") or {}
        if not isinstance(requirements, Mapping):
            raise ValueError(f"case {case.get('id')} requirements must be a mapping")
        missing_required = [
            str(capability)
            for capability, mode in requirements.items()
            if mode == "required" and not availability.get(str(capability), False)
        ]
        if missing_required:
            # A location-consuming case may also request API capture.  Report
            # the safety precondition first; otherwise a dead Frida tunnel is
            # misdiagnosed as optional observability rather than the concrete
            # reason no fulfillment action may start.
            capability = min(missing_required, key=lambda name: (_CAPABILITY_PRIORITY.get(name, 99), name))
            plan.append({
                "case": case,
                "state": "blocked_capability",
                "blocker": blocker(
                    "capability",
                    f"{capability}_unavailable",
                    capability=capability,
                    recovery=f"restore capability {capability}",
                ),
            })
            boundary_before_next = True
            continue
        if boundary_before_next:
            case["_break_shared_precondition_before"] = True
            boundary_before_next = False
        advisory_missing = any(
            mode == "advisory" and not availability.get(str(capability), False)
            for capability, mode in requirements.items()
        )
        plan.append({"case": case, "state": "runnable_advisory_pending" if advisory_missing else "runnable"})
    return plan
