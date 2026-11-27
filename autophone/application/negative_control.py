"""Generic negctl: produce discrimination evidence through the Control Plane.

One invocation runs one declared negative control
(``apps/<app>/discrimination/negative_controls.yaml``) for one Case:

1. plan on the host: load the declaration, resolve it against the Recipe the
   Case would run now (draft Trial Recipe when ``recipes/drafts/<id>.yaml``
   exists, otherwise the live Recipe), and build the two Run Specs;
2. pin the target Rig to the one the Run Spec's ``device`` names (an explicit
   ``--rig-id`` must match it), submit the positive Run as a Control Plane
   Assignment and wait for it;
3. only when the positive Case passed, submit the negative Run (same selector
   plus ``negative_control``) to the same target Rig and wait for it;
4. judge the pair (``autophone.assets.negative_control.judge_negative_control``)
   and merge the probe into ``runs/negctl/<build_label>/artifact.json``, the
   file the Promotion Gate reads.

Device work goes only through ``submit_assignment``; the Rig Agent executes
each Run under the ScreenLock like any other Assignment.  There is no direct
device path here, and a missing Control Plane is refused by the caller.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.assets.negative_control import (
    NegativeControlError,
    declaration_sha256,
    find_negative_control,
    judge_negative_control,
    resolve_negative_control,
    write_probe,
)
from autophone.assets.promotion_identity import (
    RECIPE_SOURCE_DRAFT,
    RECIPE_SOURCE_LIVE,
    recipe_path,
)
from autophone.logging import get_logger

logger = get_logger(__name__)

TERMINAL_ASSIGNMENT_STATUSES = frozenset(
    {"passed", "failed", "error", "blocked", "cancelled", "indeterminate"}
)
DEFAULT_TIMEOUT_S = 7200.0
DEFAULT_POLL_INTERVAL_S = 2.0


class NegativeControlDispatchError(RuntimeError):
    """An Assignment could not be submitted or did not settle."""

    def __init__(self, code: str, detail: str, *, assignment_id: str | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.assignment_id = assignment_id


@dataclass(frozen=True)
class NegativeControlRequest:
    app: str
    case_id: str
    control_id: str
    device: str
    target_rig_id: str | None = None
    priority: str = "normal"
    requested_by: str = "negctl"
    trial_confirmation: list[dict[str, Any]] | None = None


@dataclass
class NegativeControlPlan:
    app: str
    case_id: str
    control: dict[str, Any]
    declaration_sha256: str
    build_label: str
    recipe_source: str
    selector: dict[str, Any]
    base_steps: list[dict[str, Any]]
    perturbed_steps: list[dict[str, Any]]
    record: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "case_id": self.case_id,
            "control_id": self.control["id"],
            "declaration_sha256": self.declaration_sha256,
            "build_label": self.build_label,
            "recipe_source": self.recipe_source,
            "case_selector": copy.deepcopy(self.selector),
            **copy.deepcopy(self.record),
            "base_steps": copy.deepcopy(self.base_steps),
            "perturbed_steps": copy.deepcopy(self.perturbed_steps),
        }


def assignment_rig_id(assignment: Mapping[str, Any] | None) -> str | None:
    """The Rig that actually executed an Assignment (current attempt, else binding)."""
    if not assignment:
        return None
    attempt = assignment.get("attempt")
    rig = (attempt or {}).get("rig_id") if isinstance(attempt, Mapping) else None
    rig = rig or assignment.get("bound_rig_id")
    return str(rig) if rig else None


def _load_case(profile: Any, case_id: str) -> dict[str, Any]:
    from autophone.execution.engine.cases_loader import load_cases

    for row in load_cases(profile.cases_path()):
        if str(row.get("id") or "").upper() == case_id:
            return row
    raise NegativeControlError(f"Case {case_id} is not in the Case Registry")


def plan_negative_control(profile: Any, case_id: str, control_id: str) -> NegativeControlPlan:
    """Resolve a declaration against the Recipe the Case would run now (no device)."""
    import yaml

    key = str(case_id).upper()
    build_label = getattr(profile, "build_label", None)
    if not build_label:
        raise NegativeControlError(
            f"app {profile.app} has no build_label; discrimination evidence must be build-stamped"
        )
    control = find_negative_control(profile, key, control_id)
    case = _load_case(profile, key)
    draft = recipe_path(profile, key, source=RECIPE_SOURCE_DRAFT)
    if draft.is_file():
        source = RECIPE_SOURCE_DRAFT
        path = draft
        selector: dict[str, Any] = (
            {"ids": [key], "statuses": ["draft"]}
            if case.get("status") == "draft"
            else {"ids": [key], "trial_draft_recipes": True}
        )
    else:
        source = RECIPE_SOURCE_LIVE
        path = recipe_path(profile, key, source=RECIPE_SOURCE_LIVE)
        if case.get("status") != "active":
            raise NegativeControlError(f"Case {key} has no draft Recipe and is not active")
        selector = {"ids": [key]}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise NegativeControlError(f"Recipe {path} is unreadable: {exc}") from exc
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list) or not steps:
        raise NegativeControlError(f"Recipe {path} has no steps")
    from autophone.assets.operations import load_operations

    resolved = resolve_negative_control(
        steps, control,
        operations=load_operations(profile) if control["perturbation"]["kind"] == "flip_witness" else None,
        app=profile.app,
    )
    return NegativeControlPlan(
        app=str(profile.app),
        case_id=key,
        control=control,
        declaration_sha256=declaration_sha256(control),
        build_label=str(build_label),
        recipe_source=source,
        selector=selector,
        base_steps=copy.deepcopy(steps),
        perturbed_steps=resolved.steps,
        record=resolved.record(control, steps),
    )


class NegativeControlRunner:
    """Submit the positive/negative Run pair as Assignments and record the probe."""

    def __init__(
        self,
        *,
        control: Any,
        negctl_root: Path,
        profile_loader: Callable[[str], Any] | None = None,
        identity_gate: Callable[[Any, list[str], str | None], None] | None = None,
        run_id_factory: Callable[[str], str] | None = None,
        now: Callable[[], str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if control is None:
            raise NegativeControlDispatchError(
                "control_plane_not_configured",
                "negctl submits Runs only through the Control Plane; configure AUTOPHONE_DATABASE_URL",
            )
        from autophone.assets.apps import load_app_profile
        from autophone.execution.run_service import mint_run_id
        from autophone.util import now_iso

        self.control = control
        self.negctl_root = Path(negctl_root)
        self.profile_loader = profile_loader or load_app_profile
        self.identity_gate = identity_gate
        self.run_id_factory = run_id_factory or mint_run_id
        self.now = now or now_iso
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        self.sleep = sleep
        self.clock = clock

    # ── specs ──────────────────────────────────────────────────────────────
    def _spec(self, request: NegativeControlRequest, plan: NegativeControlPlan,
              *, negative: bool) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "schema_version": "v1",
            "run_id": self.run_id_factory(plan.app),
            "type": "regression",
            "app": plan.app,
            "device": request.device,
            "case_selector": copy.deepcopy(plan.selector),
            "auto_login": True,
            "planes": {
                "ui": {"mode": "required"},
                "function": {"mode": "disabled"},
                "api": {"mode": "disabled"},
                "telemetry": {"mode": "disabled"},
            },
            "requested_by": request.requested_by,
            "requested_at": self.now(),
        }
        if request.trial_confirmation is not None:
            # Both Runs carry the reviewed snapshot. Never refresh it after the
            # positive Run: asset drift must require another explicit acknowledgement.
            spec["trial_confirmation"] = copy.deepcopy(request.trial_confirmation)
        if negative:
            spec["negative_control"] = {
                "case_id": plan.case_id,
                "control_id": plan.control["id"],
                "declaration_sha256": plan.declaration_sha256,
            }
        return spec

    # ── Control Plane ──────────────────────────────────────────────────────
    def _submit_and_wait(self, spec: dict[str, Any], request: NegativeControlRequest,
                         *, label: str, target_rig_id: str | None,
                         ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        from autophone.contracts import validate_or_raise
        from autophone.execution.dispatch import run_capabilities
        from autophone.assets.negative_control import validate_negative_control_request

        validate_or_raise("run-spec.v1.json", spec)
        validate_negative_control_request(spec)
        capabilities = run_capabilities(spec)
        if self.identity_gate is not None:
            self.identity_gate(self.control, capabilities, target_rig_id)
        plan = {
            "kind": "run",
            "requires_rig": True,
            "priority": request.priority,
            "target_rig_id": target_rig_id,
            "required_capabilities": capabilities,
            "run_spec": spec,
        }
        run_id = str(spec["run_id"])
        try:
            assignment = self.control.submit_assignment(plan, f"negctl:{label}:{run_id}")
        except Exception as exc:  # noqa: BLE001 - reported as a refused submission
            raise NegativeControlDispatchError(
                "assignment_submit_failed", f"{label} Run {run_id}: {type(exc).__name__}: {exc}",
            ) from exc
        assignment_id = str(assignment.get("assignment_id") or "")
        logger.info("negctl %s Run 已提交 run_id=%s assignment=%s target_rig=%s",
                    label, run_id, assignment_id, target_rig_id)
        deadline = self.clock() + self.timeout_s
        while str(assignment.get("status") or "") not in TERMINAL_ASSIGNMENT_STATUSES:
            if self.clock() >= deadline:
                raise NegativeControlDispatchError(
                    "assignment_timeout",
                    f"{label} assignment {assignment_id} did not settle within "
                    f"{self.timeout_s:.0f}s (status={assignment.get('status')!r})",
                    assignment_id=assignment_id,
                )
            self.sleep(self.poll_interval_s)
            assignment = self.control.get_assignment(assignment_id)
        result = assignment.get("result")
        run_result = (result or {}).get("run_result") if isinstance(result, Mapping) else None
        logger.info("negctl %s Run 结束 run_id=%s assignment=%s status=%s",
                    label, run_id, assignment_id, assignment.get("status"))
        if isinstance(run_result, Mapping):
            projected = dict(run_result)
            if (
                isinstance(result, Mapping)
                and result.get("candidate_identity") is not None
                and "candidate_identity" not in projected
            ):
                projected["candidate_identity"] = copy.deepcopy(result["candidate_identity"])
            return dict(assignment), projected
        return dict(assignment), None

    def _pinned_target_rig(self, request: NegativeControlRequest) -> str:
        """Resolve the Rig the Run Spec's ``device`` names (BL-215).

        Same rule as the CLI ``run``/``replay``/``operation``/``impact`` entries and
        ``POST /runs``: the device must name exactly one registered Rig, and an
        explicit ``--rig-id`` must be that Rig.  Otherwise the Run Result would
        record one device while another Rig executed it.  Both refusals happen
        before any Assignment is submitted.
        """
        device = str(request.device or "")
        try:
            resolved = self.control.resolve_device_rig(device)
            if not isinstance(resolved, str) or not resolved:
                raise ValueError("registered Rig ID is missing")
        except Exception as exc:  # noqa: BLE001 - any resolution failure refuses the run
            raise NegativeControlDispatchError(
                "device_rig_unresolved",
                f"device {device!r} does not name exactly one registered Rig; "
                f"no Run submitted ({type(exc).__name__}: {exc})",
            ) from exc
        if request.target_rig_id and str(request.target_rig_id) != resolved:
            raise NegativeControlDispatchError(
                "target_rig_device_mismatch",
                f"--rig-id {request.target_rig_id!r} is not the Rig {resolved!r} that device "
                f"{device!r} names; no Run submitted",
            )
        return resolved

    # ── entry point ────────────────────────────────────────────────────────
    def run(self, request: NegativeControlRequest) -> dict[str, Any]:
        profile = self.profile_loader(request.app)
        plan = plan_negative_control(profile, request.case_id, request.control_id)
        target_rig_id = self._pinned_target_rig(request)
        positive_spec = self._spec(request, plan, negative=False)
        positive_assignment, positive = self._submit_and_wait(
            positive_spec, request, label="positive", target_rig_id=target_rig_id,
        )
        positive_rig = assignment_rig_id(positive_assignment)
        if positive is None:
            positive = {"run_id": positive_spec["run_id"],
                        "status": positive_assignment.get("status"), "cases": []}
        positive_case = next(
            (row for row in positive.get("cases") or []
             if str(row.get("id") or "").upper() == plan.case_id),
            None,
        )
        negative_assignment: dict[str, Any] | None = None
        negative: dict[str, Any] | None = None
        positive_green = positive_case is not None and positive_case.get("status") == "passed"
        if positive_green and positive_rig is None:
            # 读不到正向 Run 实际执行的 Rig 时无法把反向 Run 钉到同一台，不提交反向 Run。
            logger.warning("negctl 正向 Assignment 未记录执行 Rig，不提交反向 Run")
        elif positive_green:
            negative_spec = self._spec(request, plan, negative=True)
            # 反向 Run 固定派到正向 Run 实际执行的 Rig（未指定 --rig-id 时同样如此）。
            negative_assignment, negative = self._submit_and_wait(
                negative_spec, request, label="negative", target_rig_id=positive_rig,
            )
            if negative is None:
                negative = {"run_id": negative_spec["run_id"],
                            "status": negative_assignment.get("status"), "cases": []}
        else:
            logger.warning("negctl 正向 Run 未通过（case_status=%s），不提交反向 Run",
                           (positive_case or {}).get("status"))
        generated_at = self.now()
        probe = judge_negative_control(
            app=plan.app,
            control=plan.control,
            build_label=plan.build_label,
            positive=positive,
            negative=negative,
            positive_assignment_id=str(positive_assignment.get("assignment_id") or "") or None,
            negative_assignment_id=(
                str((negative_assignment or {}).get("assignment_id") or "") or None
            ),
            target_rig_id=target_rig_id,
            positive_rig_id=positive_rig,
            negative_rig_id=(
                assignment_rig_id(negative_assignment) if negative_assignment is not None else None
            ),
            generated_at=generated_at,
            planned_record=plan.record,
        )
        artifact = write_probe(self.negctl_root, probe, generated_at=generated_at)
        logger.info("negctl %s/%s 结论：%s", plan.case_id, plan.control["id"], probe["finding"])
        return {"probe": probe, "artifact": str(artifact), "plan": plan.summary()}


__all__ = [
    "NegativeControlDispatchError",
    "NegativeControlPlan",
    "NegativeControlRequest",
    "NegativeControlRunner",
    "TERMINAL_ASSIGNMENT_STATUSES",
    "assignment_rig_id",
    "plan_negative_control",
]
