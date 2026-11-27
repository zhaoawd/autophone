"""Shared result contract for capability-aware regression runs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


BLOCKER_SCOPES = frozenset({"asset", "rig", "capability", "fixture"})


class CaseBlocked(RuntimeError):
    """Stop one case without converting a known prerequisite gap into a failure."""

    def __init__(self, payload: Mapping[str, Any], *, detail: str = "") -> None:
        self.blocker = dict(payload)
        self.detail = detail or f"{self.blocker.get('scope', 'case')} blocked: {self.blocker.get('code', '')}"
        super().__init__(self.detail)


def blocker(
    scope: str,
    code: str,
    *,
    capability: str | None = None,
    recovery: str = "",
    evidence_ref: str | None = None,
) -> dict[str, str]:
    """Return a schema-shaped, machine-routable blocker payload."""
    if scope not in BLOCKER_SCOPES:
        raise ValueError(f"unknown blocker scope: {scope}")
    if not code:
        raise ValueError("blocker code is required")
    payload = {"scope": scope, "code": code, "recovery": recovery}
    if capability:
        payload["capability"] = capability
    if evidence_ref:
        payload["evidence_ref"] = evidence_ref
    return payload


# 登录过程中被测 App 存活检测失败（BL-083 2026-10-11 第七轮）：`do_login` 的 stage → blocker code。
# pid 消失记 `app_process_exited`；pid 仍在但画面已不是该 App 的登录页记 `app_not_foreground`。
# 两者都是被测 App 进程或前台不可用，按 rig 范围阻断，不计为产品断言失败。
APP_LIVENESS_BLOCKER_CODES = {
    "app_crashed": "app_process_exited",
    "app_left_foreground": "app_not_foreground",
}


def app_liveness_blocker(stage: str) -> dict[str, str] | None:
    """`do_login` 存活检测 stage 对应的 blocker；其他 stage 返回 None。"""
    code = APP_LIVENESS_BLOCKER_CODES.get(str(stage or ""))
    if code is None:
        return None
    recovery = (
        "the app under test exited during login; collect the guest <process>-*.ips crash report "
        "and the run's planes/crash events, relaunch the app, then rerun"
        if code == "app_process_exited" else
        "the screen left the app under test during login; read the decisive frame, return to "
        "the app (Home + relaunch, no kill of system apps), then rerun"
    )
    return blocker("rig", code, capability="app_ui", recovery=recovery)


def failure_reasons(cases: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return independent product and asset findings without losing coexistence."""
    reasons: set[str] = set()
    for case in cases:
        if case.get("status") == "failed":
            reasons.add("product")
        raw_blocker = case.get("blocker")
        if isinstance(raw_blocker, Mapping) and raw_blocker.get("scope") == "asset":
            reasons.add("asset")
    return sorted(reasons)


def reduce_regression_status(
    *,
    base_rig_blocked: bool,
    orchestration_error: bool,
    required_planes: Iterable[Mapping[str, Any]],
    cases: Iterable[Mapping[str, Any]],
) -> str:
    """Apply the single status precedence used by every regression consumer."""
    case_list = list(cases)
    if base_rig_blocked:
        return "blocked"
    if orchestration_error:
        return "error"
    if any(plane.get("mode") == "required" and plane.get("verdict") in {"pending", "captured"}
           for plane in required_planes):
        return "pending"
    if any(plane.get("mode") == "required" and plane.get("verdict") == "fail"
           for plane in required_planes):
        return "failed"
    if any(case.get("status") == "error" for case in case_list):
        return "error"
    if failure_reasons(case_list):
        return "failed"
    if any(case.get("status") == "blocked" for case in case_list):
        return "partial"
    if case_list and all(case.get("status") == "passed" for case in case_list):
        return "passed"
    return "failed"


def regression_exit_code(status: str) -> int:
    """Map terminal regression statuses to the documented CLI exit code."""
    try:
        return {
            "passed": 0,
            "failed": 1,
            "partial": 3,
            "blocked": 4,
            "error": 5,
            "pending": 6,
        }[status]
    except KeyError as exc:
        raise ValueError(f"unknown regression status: {status}") from exc
