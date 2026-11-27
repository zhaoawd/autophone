"""Golden path declarations, host-side plans and Run evidence verification.

A golden path (usecase assetization §21 stage C) binds one Case to the full
closed loop: Preflight → Fixture prepare → Fixture Readback → Body → Oracle →
Cleanup → Report → Promotion.  This module only plans and verifies:

* ``plan_golden_path`` projects the preparation card into ordered stages and
  the Run Spec fields a caller would submit: the explicit selector and the
  ``golden_path`` marker (``autophone.assets.golden_paths``).  It never submits
  a Run.
* ``verify_golden_path_run`` reads one settled Run directory and reports, per
  stage, whether the recorded evidence is present, and classifies the outcome
  as product failure, Fixture failure, asset drift or Rig error.  The Run must
  carry a marker for this path; a Draft Trial without a marker is accepted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from autophone.assets.build_identity import build_identity_unconfirmed, label_build_mismatch
from autophone.assets.fixture_requirements import (
    CLEANUP_READBACK_EVIDENCE_NAME,
    FIXTURE_READBACK_EVIDENCE_NAME,
    cleanup_runtime_states,
    declared_fixtures,
    load_cleanup_evidence,
    fixture_runtime_states,
    load_fixture_requirements,
    load_readback_evidence,
)
from autophone.assets.golden_paths import (  # noqa: F401 - re-exported for callers
    BINDING_ABSENT,
    BINDING_DECLARATION_CHANGED,
    BINDING_MATCHED,
    GOLDEN_PATH_SCHEMA,
    RUN_MARKER_FIELD,
    GoldenPathError,
    find_golden_path,
    golden_path_selector,
    golden_paths_path,
    load_golden_paths,
    run_binding,
    run_marker,
)
from autophone.assets.promotion_identity import selector_is_draft_trial
from autophone.paths import APP_ASSET_ROOT
from autophone.util import read_json

GOLDEN_PATH_PLAN_SCHEMA = "autophone.golden-path-plan/1"
GOLDEN_PATH_VERIFICATION_SCHEMA = "autophone.golden-path-verification/1"
STAGES = (
    "preflight", "fixture_prepare", "fixture_readback", "body",
    "oracle", "cleanup", "report", "promotion",
)
OUTCOMES = frozenset({
    "passed", "product_failure", "fixture_failure", "asset_drift", "rig_error", "incomplete",
})


def _find_path(profile: Any, path_id: str) -> dict[str, Any]:
    return find_golden_path(profile, path_id)


def _plan_requirements(profile: Any, case_id: str) -> list[dict[str, Any]]:
    rows = load_fixture_requirements(profile)["cases"].get(str(case_id).upper(), [])
    return declared_fixtures(rows)


def _cleanup_stage(run_dir: Path, requirements: list[dict[str, Any]], *, case_id: str,
                   run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Cleanup stage from ``cleanup-readback.json`` (BL-083 第八轮).

    No Fixture declares ``cleanup``: ``unverified`` as before (no independent
    evidence field).  Declared: ``present`` only when the file matches the
    Artifact Manifest and every declared cleanup is ``verified`` against the
    current declaration digest; ``failed`` when any cleanup or its Readback
    failed; ``missing`` when the evidence or an entry is absent; ``unverified``
    otherwise (manifest digest missing or mismatched, declaration drift).
    """
    if not any(req.get("cleanup_digest") for req in requirements):
        return {"state": "unverified", "detail": "no cleanup declared; no independent cleanup evidence field"}, None
    evidence, manifest_check = load_cleanup_evidence(run_dir)
    states = cleanup_runtime_states(requirements, evidence, case_id=case_id, run_id=run_id)
    detail = "; ".join(f"{s['fixture_id']}={s['state']}" + (f" ({s['reason']})" if s["reason"] else "")
                       for s in states)
    names = {s["state"] for s in states}
    if "failed" in names:
        state = "failed"
    elif "missing" in names:
        state = "missing"
    elif "unverified" in names or manifest_check.get("state") != "match":
        state = "unverified"
        if manifest_check.get("state") != "match":
            detail += f"; manifest={manifest_check.get('state')}"
    else:
        state = "present"
    return {"state": state, "detail": detail}, manifest_check


def plan_golden_path(
    app: str,
    path_id: str,
    *,
    root: Path = APP_ASSET_ROOT,
    state_root: Path | None = None,
    run_root: Path | None = None,
) -> dict[str, Any]:
    """Ordered stage plan for one golden path.  Never submits or executes a Run."""
    from autophone.assets.apps import load_app_profile
    from autophone.knowledge.preparation_card import build_preparation_card

    profile = load_app_profile(app, root=root, state_root=state_root)
    declared = _find_path(profile, path_id)
    card = build_preparation_card(
        app, declared["case_id"], trial_draft_recipes=declared["trial_draft_recipes"],
        root=root, state_root=state_root, run_root=run_root,
    )
    blockers = list(card.get("blockers") or [])
    allowed = set(profile.allowed_effects or [])
    forbidden_allowed = sorted(allowed & set(declared["forbidden_effects"]))
    if forbidden_allowed:
        blockers.append("forbidden_effect_allowed")
    case = card.get("case") or {}
    lifecycle = str(case.get("status") or "")
    selector = golden_path_selector(declared, lifecycle)
    marker = run_marker(declared)
    rows = {row["key"]: row for row in card.get("checklist") or []}
    fixtures = card.get("fixtures") or {}

    def state_of(*keys: str) -> str:
        states = [rows[key]["state"] for key in keys if key in rows]
        for state in ("blocked", "unverified", "attention"):
            if state in states:
                return state
        if states and all(state == "not_applicable" for state in states):
            return "not_applicable"
        return "ready"

    stages = [
        {"stage": "preflight", "state": state_of("lifecycle", "target", "automation"),
         "required_evidence": [f"run-spec.json:{RUN_MARKER_FIELD}.id={declared['id']}",
                               "platform-run-result.json:package.build_identity_match=match"]},
        {"stage": "fixture_prepare", "state": state_of("fixture_contract"),
         "required_evidence": [f"{FIXTURE_READBACK_EVIDENCE_NAME}:provider_ref"]
         if fixtures.get("applicable") else []},
        {"stage": "fixture_readback", "state": state_of("fixture_readback"),
         "required_evidence": [f"{FIXTURE_READBACK_EVIDENCE_NAME}:status=ready,readback_ref,context"]
         if fixtures.get("applicable") else []},
        {"stage": "body", "state": state_of("recipe", "effect"),
         "required_evidence": ["platform-run-result.json:cases[].exec_via=recipe",
                               "platform-run-result.json:cases[].draft_recipe_identity"
                               if selector_is_draft_trial(selector)
                               else "platform-run-result.json:cases[].recipe_identity"]},
        {"stage": "oracle", "state": state_of("oracle"),
         "required_evidence": ["platform-run-result.json:cases[].status",
                               "platform-run-result.json:planes"]},
        {"stage": "cleanup", "state": "unverified",
         "required_evidence": (
             [f"{CLEANUP_READBACK_EVIDENCE_NAME}:status=verified,cleanup_ref,readback_ref"]
             if any(req.get("cleanup_digest") for req in _plan_requirements(profile, declared["case_id"]))
             else ["teardown / post_case_cleanup 执行记录（Fixture 未声明 cleanup，无独立证据字段）"])},
        {"stage": "report", "state": "ready",
         "required_evidence": ["platform-run-result.json:run_id"]},
        {"stage": "promotion", "state": "unverified",
         "required_evidence": ["case-draft-review.json", "连续通过与区分力（转正闸）"]},
    ]
    return {
        "schema": GOLDEN_PATH_PLAN_SCHEMA,
        "executes": False,
        "app": app,
        "path": declared,
        "trial_selector": selector,
        # The Run Spec fields a caller submits: the selector plus the golden
        # path marker.  Only a marked Run gets Fixture Readback bound for a
        # live Recipe Case; the Runner and the verification check the marker.
        "run_spec": {"case_selector": selector, RUN_MARKER_FIELD: marker},
        "submit_command": (
            f"uv run --group platform python3 -m autophone.cli --app {app} "
            f"run --golden-path {declared['id']}"
        ),
        "requires_confirmation": bool((card.get("effects") or {}).get("requires_confirmation")),
        "forbidden_effects_allowed": forbidden_allowed,
        "stages": stages,
        "blockers": list(dict.fromkeys(blockers)),
        "preparation_card": card,
    }


def classify_case_outcome(case: dict[str, Any] | None, *, run_status: str | None) -> str:
    """Map one recorded Case result to a golden path outcome class."""
    if case is None:
        return "rig_error" if run_status in {"blocked", "error"} else "incomplete"
    status = str(case.get("status") or "")
    blocker = case.get("blocker") if isinstance(case.get("blocker"), dict) else {}
    scope = str(blocker.get("scope") or "")
    if status == "passed":
        return "passed"
    if scope == "fixture":
        return "fixture_failure"
    if scope == "asset":
        return "asset_drift"
    if scope in {"rig", "capability"} or status == "error":
        return "rig_error"
    if status == "failed":
        return "product_failure"
    return "incomplete"


def verify_golden_path_run(
    app: str,
    path_id: str,
    run_dir: Path,
    *,
    root: Path = APP_ASSET_ROOT,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Check one settled Run against the golden path evidence contract (read only)."""
    from autophone.assets.apps import load_app_profile
    from autophone.assets.promotion_identity import (
        RECIPE_SOURCE_DRAFT,
        RECIPE_SOURCE_LIVE,
        current_recipe_identity,
        run_matches_recipe_identity,
    )
    from autophone.contracts import ContractError

    profile = load_app_profile(app, root=root, state_root=state_root)
    declared = _find_path(profile, path_id)
    case_id = declared["case_id"]
    spec = read_json(run_dir / "run-spec.json") or {}
    result = read_json(run_dir / "platform-run-result.json") or {}
    run_id = str(result.get("run_id") or spec.get("run_id") or run_dir.name)
    stages: dict[str, dict[str, Any]] = {
        name: {"stage": name, "state": "missing", "detail": ""} for name in STAGES
    }
    manifest_check: dict[str, Any] | None = None
    # The Run must have been submitted for this golden path (run-spec.json
    # ``golden_path`` marker).  A Draft Trial without a marker is still read:
    # Draft Trials bound Fixture Readback before the marker existed.
    binding = _binding(spec, declared)
    binding_ok = binding["accepted"]
    draft_trial = binding["draft_trial"]
    if not result:
        stages["report"]["detail"] = "platform-run-result.json missing"
        return _verification(app, declared, run_id, stages, "incomplete", None,
                             run_binding=binding)
    if str(result.get("app") or spec.get("app") or "") != app:
        stages["report"].update(state="failed", detail="run app does not match")
        return _verification(app, declared, run_id, stages, "asset_drift", None,
                             run_binding=binding)
    stages["report"].update(state="present", detail=run_id)

    package = result.get("package") if isinstance(result.get("package"), dict) else {}
    match = package.get("build_identity_match")
    # match at an accepted granularity: build_marker, or info_plist only for a
    # source_revision.kind=unknown Build (BL-241).
    unconfirmed = build_identity_unconfirmed(package)
    label_ok = profile.build_label is not None and package.get("profile_build_label") == profile.build_label
    # The compared Build must be the one the label names (BL-190).
    label_build = label_build_mismatch(profile.build_label, package)
    stages["preflight"].update(
        state=(
            "present"
            if unconfirmed is None and label_ok and label_build is None and binding_ok
            else "failed"
        ),
        detail=(
            f"build_identity_match={match}; "
            f"comparison_granularity={package.get('comparison_granularity')}; "
            + (f"{unconfirmed}; " if unconfirmed and match == "match" else "")
            + f"profile_build_label={package.get('profile_build_label')}; "
            f"comparison_build_id={package.get('comparison_build_id')}"
            + (f"; {label_build}" if label_build else "")
            + f"; golden_path_binding={binding['state']}"
            + ("" if binding_ok else f" ({binding['detail']})")
        ),
    )
    case = next(
        (row for row in result.get("cases") or [] if str(row.get("id") or "") == case_id),
        None,
    )
    outcome = classify_case_outcome(case, run_status=str(result.get("status") or ""))
    drift = False
    if case is not None:
        # Same identity judgement as the preparation card: a Draft Trial run is
        # matched on the draft identity, any other run on the executed live
        # Recipe identity (BL-186).  A missing or unverified record is drift.
        source = RECIPE_SOURCE_DRAFT if draft_trial else RECIPE_SOURCE_LIVE
        try:
            current = current_recipe_identity(profile, case_id, source=source)
        except ContractError:
            current = None
        recipe_run = case.get("exec_via") == "recipe"
        drift = recipe_run and not run_matches_recipe_identity(case, current)
        body_ok = recipe_run and not drift
        stages["body"].update(
            state="present" if body_ok else "failed",
            detail=(f"{source} recipe identity differs from current assets or is not recorded"
                    if drift else f"exec_via={case.get('exec_via')}"),
        )
        stages["oracle"].update(
            state="present" if case.get("status") == "passed" else "failed",
            detail=str(case.get("status")),
        )
    if drift and outcome == "passed":
        outcome = "asset_drift"

    rows = load_fixture_requirements(profile)["cases"].get(case_id.upper(), [])
    requirements = declared_fixtures(rows)
    if requirements:
        evidence, manifest_check = load_readback_evidence(run_dir)
        states = fixture_runtime_states(requirements, evidence, case_id=case_id, run_id=run_id)
        # Fixture preparation is evidenced only by an entry that references the
        # provider's recorded setup action; the Runner writes failed entries
        # without provider_ref when the Case was blocked before or during setup.
        entries = {
            str(entry.get("fixture_id")): entry
            for entry in ((evidence or {}).get("cases") or {}).get(case_id.upper()) or []
            if isinstance(entry, dict)
        }
        prepared = evidence is not None and all(
            str((entries.get(req["id"]) or {}).get("provider_ref") or "").strip()
            for req in requirements
        )
        stages["fixture_prepare"].update(state="present" if prepared else "missing")
        ready = all(state["state"] == "ready" for state in states)
        failed = any(state["state"] == "failed" for state in states)
        stages["fixture_readback"].update(
            state="present" if ready else "failed" if failed else "missing",
            detail="; ".join(f"{s['fixture_id']}={s['state']}" for s in states),
        )
        if failed and outcome in {"passed", "product_failure"}:
            outcome = "fixture_failure"
    else:
        from autophone.execution.engine.cases_loader import load_cases

        registry_case = next(
            (row for row in load_cases(profile.cases_path()) if str(row.get("id")) == case_id),
            {},
        )
        needs_fixture = registry_case.get("automation") == "needs_fixture"
        for name in ("fixture_prepare", "fixture_readback"):
            stages[name].update(
                state="missing" if needs_fixture else "not_declared",
                detail="needs_fixture Case has no Fixture declaration" if needs_fixture else "",
            )
    cleanup_stage, cleanup_manifest = _cleanup_stage(
        run_dir, requirements, case_id=case_id, run_id=run_id)
    stages["cleanup"].update(cleanup_stage)
    review = read_json(run_dir / "case-draft-review.json") or {}
    stages["promotion"].update(
        state="present" if isinstance(review.get(case_id), dict) else "missing",
        detail="per-run review only; Promotion Gate decides separately",
    )
    complete = all(
        stages[name]["state"] in {"present", "not_declared"}
        for name in ("preflight", "fixture_prepare", "fixture_readback", "body", "oracle", "report")
    )
    if outcome == "passed" and not complete:
        outcome = "incomplete"
    # A Run not accepted as this path's Run (a live Run without a marker, a
    # marker for another path, a changed declaration or an invalid marker) is
    # no evidence of this path: whatever its Case did, the result is incomplete.
    if not binding_ok:
        outcome = "incomplete"
    return _verification(app, declared, run_id, stages, outcome, case,
                         evidence_manifest=manifest_check, run_binding=binding,
                         cleanup_manifest=cleanup_manifest)


def _binding(spec: dict[str, Any], declared: dict[str, Any]) -> dict[str, Any]:
    """``run_binding`` plus whether verification accepts the Run for this path."""
    draft_trial = selector_is_draft_trial(spec.get("case_selector") or {})
    binding = run_binding(spec, declared)
    accepted = binding["state"] == BINDING_MATCHED or (
        binding["state"] == BINDING_ABSENT and draft_trial
    )
    return {**binding, "accepted": accepted, "draft_trial": draft_trial}


def _verification(
    app: str,
    declared: dict[str, Any],
    run_id: str,
    stages: dict[str, dict[str, Any]],
    outcome: str,
    case: dict[str, Any] | None,
    *,
    evidence_manifest: dict[str, Any] | None = None,
    run_binding: dict[str, Any] | None = None,
    cleanup_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": GOLDEN_PATH_VERIFICATION_SCHEMA,
        "app": app,
        "path": declared,
        "run_id": run_id,
        "outcome": outcome,
        "case_status": (case or {}).get("status"),
        "blocker": (case or {}).get("blocker"),
        "stages": [stages[name] for name in STAGES],
        # fixture-readback.json vs artifact-manifest.json (None when no Fixture is declared).
        "evidence_manifest": evidence_manifest,
        # run-spec.json ``golden_path`` marker compared with the current declaration.
        "run_binding": run_binding,
        # cleanup-readback.json vs artifact-manifest.json (None when no cleanup is declared).
        "cleanup_manifest": cleanup_manifest,
    }
