"""Declared controlled experiments and the run-control emitter (M3b producer side).

`observation_compiler` consumes `knowledge-control.json` but nothing produced it,
so a controlled contrast could only ever be hand-written next to the run it labels.
This module closes that gap from an auditable app asset.

The integrity rule that shapes the whole module: `change_as_expected` is **derived
from what the run actually did**, never accepted as a caller flag. An arm declares
which case carries it and which verdict counts as the expected direction; the arm's
own assertions encode that direction, and `result.json` says whether it held. A run
that ended in `error`/`blocked`/`skipped` yields no contrast at all rather than a
convenient `true`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.contracts import validate_or_raise, write_json
from autophone.util import read_json

CONTROL = "knowledge-control.json"
EXPERIMENT_TIMEZONE = ZoneInfo("Asia/Shanghai")

# `result.json` is raw phase2 runner evidence. It uses `pass`/`fail`, while the
# declared arm uses the platform spelling `passed`, so decisive raw verdicts are
# normalised only after validating that they belong to the runner vocabulary.
VERDICT_ALIASES = {"pass": "passed", "fail": "failed"}

RUNNER_VERDICTS = frozenset({"pass", "fail", "error", "blocked", "skipped"})
DECISIVE_VERDICTS = frozenset(VERDICT_ALIASES)


def experiments_path(app: str, *, root: Path = APP_ASSET_ROOT) -> Path:
    return root / "apps" / app / "knowledge" / "experiments.yaml"


def load_experiments(app: str, *, root: Path = APP_ASSET_ROOT) -> dict[str, Any]:
    path = experiments_path(app, root=root)
    if not path.is_file():
        return {"schema_version": "v1", "app": app, "experiments": []}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain an object")
    validate_or_raise("knowledge-experiments.v1.json", raw)
    if str(raw.get("app")) != app:
        raise ValueError(f"{path} app does not match {app}")
    ids = [str(row.get("id")) for row in raw.get("experiments") or []]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path} contains duplicate experiment ids")
    for experiment in raw.get("experiments") or []:
        arms = experiment.get("arms") or []
        arm_ids = [str(arm.get("id")) for arm in arms]
        if len(set(arm_ids)) != len(arm_ids):
            raise ValueError(f"{experiment.get('id')} contains duplicate arm ids")
        case_ids = [str((arm.get("expect") or {}).get("case_id") or "") for arm in arms]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError(f"{experiment.get('id')} arms need unique expect.case_id values")
        for arm, expected_case_id in zip(arms, case_ids, strict=True):
            for binding in arm.get("bindings") or []:
                bound_case_id = str((binding.get("match") or {}).get("case_id") or "")
                if bound_case_id != expected_case_id:
                    raise ValueError(
                        f"{experiment.get('id')} arm {arm.get('id')} binding case_id "
                        f"{bound_case_id or '<missing>'} must equal expect.case_id "
                        f"{expected_case_id}"
                    )
        targets = {str(arm.get("target_input")) for arm in arms}
        if len(targets) < 2:
            raise ValueError(
                f"{experiment.get('id')} needs at least two distinct target_input values"
            )
    return raw


def find_arm(app: str, experiment_id: str, arm_id: str, *, root: Path = APP_ASSET_ROOT
             ) -> tuple[dict[str, Any], dict[str, Any]]:
    document = load_experiments(app, root=root)
    for experiment in document.get("experiments") or []:
        if str(experiment.get("id")) != experiment_id:
            continue
        for arm in experiment.get("arms") or []:
            if str(arm.get("id")) == arm_id:
                return experiment, arm
        raise ValueError(f"experiment {experiment_id} has no arm {arm_id}")
    raise ValueError(f"{app} declares no experiment {experiment_id}")


def _validate_run_app(run_dir: Path, expected_app: str) -> None:
    identities: list[tuple[str, str]] = []
    for name in ("run-spec.json", "platform-run-result.json"):
        document = read_json(run_dir / name)
        value = document.get("app") if isinstance(document, dict) else None
        if isinstance(value, str) and value:
            identities.append((name, value))

    if not identities:
        raise ValueError(
            f"{run_dir} has no usable app identity in run-spec.json or "
            "platform-run-result.json"
        )
    mismatches = [(name, value) for name, value in identities if value != expected_app]
    if mismatches:
        detail = ", ".join(f"{name}={value}" for name, value in mismatches)
        raise ValueError(
            f"{run_dir} belongs to a different app ({detail}); expected {expected_app}"
        )


def _observed_case(run_dir: Path, case_id: str) -> dict[str, Any]:
    result = read_json(run_dir / "result.json")
    if not isinstance(result, dict) or not result.get("cases"):
        raise ValueError(f"{run_dir} has no finished result.json to derive a contrast from")
    matched = [case for case in result["cases"] if str(case.get("id")) == case_id]
    if not matched:
        raise ValueError(f"{run_dir} result.json does not contain case {case_id}")
    if len(matched) > 1:
        raise ValueError(f"{run_dir} result.json contains case {case_id} more than once")
    return matched[0]


def observed_verdict(run_dir: Path, case_id: str) -> str:
    """Read the arm's decisive verdict out of the run's own phase2 result."""
    raw = str(_observed_case(run_dir, case_id).get("status") or "")
    if raw not in RUNNER_VERDICTS:
        raise ValueError(
            f"case {case_id} has unsupported result.json verdict {raw or 'unknown'}; "
            f"expected one of {sorted(RUNNER_VERDICTS)}"
        )
    if raw not in DECISIVE_VERDICTS:
        raise ValueError(
            f"case {case_id} ended {raw or 'unknown'}; only "
            f"{sorted(DECISIVE_VERDICTS)} carry a contrast direction"
        )
    return VERDICT_ALIASES[raw]


def _experiment_instance(run_dir: Path, case_id: str) -> str:
    """Derive the immutable experiment instance from the target Case action date."""
    case = _observed_case(run_dir, case_id)
    actions = case.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError(
            f"case {case_id} has no actions with recorded_at to derive experiment_instance"
        )
    dates: set[str] = set()
    for index, action in enumerate(actions, 1):
        value = action.get("recorded_at") if isinstance(action, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"case {case_id} action {index} has no recorded_at; "
                "cannot derive experiment_instance"
            )
        try:
            recorded_at = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"case {case_id} action {index} has invalid recorded_at {value!r}"
            ) from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError(
                f"case {case_id} action {index} recorded_at must include a timezone"
            )
        dates.add(recorded_at.astimezone(EXPERIMENT_TIMEZONE).date().isoformat())
    if len(dates) != 1:
        raise ValueError(
            f"case {case_id} actions span multiple execution dates {sorted(dates)}; "
            "cannot derive one experiment_instance"
        )
    return next(iter(dates))


def _reject_other_arm_cases(
    run_dir: Path,
    experiment: dict[str, Any],
    selected_case_id: str,
) -> None:
    result = read_json(run_dir / "result.json") or {}
    executed = {
        str(case.get("id") or "")
        for case in result.get("cases") or []
        if isinstance(case, dict)
    }
    other_arm_cases = {
        str((declared.get("expect") or {}).get("case_id") or "")
        for declared in experiment.get("arms") or []
    } - {selected_case_id}
    conflicts = sorted(executed & other_arm_cases)
    if conflicts:
        raise ValueError(
            f"{run_dir} also executed other arm case(s) {conflicts}; "
            "arm assignment must be isolated before observing results"
        )


def build_control(
    experiment: dict[str, Any],
    arm: dict[str, Any],
    verdict: str,
    *,
    experiment_instance: str,
) -> dict[str, Any]:
    contrast = {
        "group": str(experiment["contrast_group"]),
        "claim_id": str(experiment["claim_id"]),
        "experiment_id": str(experiment["id"]),
        "experiment_instance": experiment_instance,
        "target_input": str(arm["target_input"]),
        "change_as_expected": verdict == str(arm["expect"]["verdict"]),
    }
    bindings: list[dict[str, Any]] = []
    for declared in arm["bindings"]:
        binding: dict[str, Any] = {
            "match": dict(declared["match"]),
            "contrast": dict(contrast),
        }
        if declared.get("independent_readback") is True:
            binding["independent_readback"] = True
        bindings.append(binding)
    payload = {"schema_version": "v1", "bindings": bindings}
    validate_or_raise("knowledge-run-control.v1.json", payload)
    return payload


def emit_control(
    run_dir: Path,
    *,
    app: str,
    experiment_id: str,
    arm_id: str,
    root: Path = APP_ASSET_ROOT,
) -> dict[str, Any]:
    """Write `knowledge-control.json` for one arm of one declared experiment.

    Refuses to relabel a run that already carries a different control document —
    an arm assignment decided after seeing the outcome is not a controlled
    experiment.
    """
    experiment, arm = find_arm(app, experiment_id, arm_id, root=root)
    _validate_run_app(run_dir, app)
    case_id = str(arm["expect"]["case_id"])
    verdict = observed_verdict(run_dir, case_id)
    experiment_instance = _experiment_instance(run_dir, case_id)
    payload = build_control(
        experiment, arm, verdict, experiment_instance=experiment_instance,
    )
    path = run_dir / CONTROL
    if path.is_file():
        existing = read_json(path)
        if existing != payload:
            raise ValueError(
                f"{path} already exists with different bindings; refusing to relabel this run"
            )
    _reject_other_arm_cases(run_dir, experiment, case_id)
    write_json(path, payload)
    return {
        "control": str(path),
        "experiment": experiment_id,
        "experiment_instance": experiment_instance,
        "claim_id": str(experiment["claim_id"]),
        "arm": arm_id,
        "case_id": str(arm["expect"]["case_id"]),
        "expected_verdict": str(arm["expect"]["verdict"]),
        "observed_verdict": verdict,
        "target_input": str(arm["target_input"]),
        "change_as_expected": payload["bindings"][0]["contrast"]["change_as_expected"],
        "binding_count": len(payload["bindings"]),
    }
