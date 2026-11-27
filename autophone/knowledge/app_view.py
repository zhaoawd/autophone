"""App knowledge view (usecase assetization §21 stage A).

One row per Case relating its business family, Recipe, Operations, declared
Fixtures, Oracle, Screens and preparation state, plus an App-level summary of
blocked Cases grouped by blocker code.

It is a read model over existing assets.  Relations come from knowledge query 1
and readiness from the preparation card; this module does not copy asset
content into a second store, does not re-judge readiness and never executes a
Trial, Setup or Readback.

Schema ``autophone.app-knowledge-view/2`` (Case Family proposal §4.7) resolves
each row's Case Family, Stage and Business Chain from the App's
``business_chains.yaml`` declaration and the Case ``family_id``.  ``family.state``
is one of ``declared``, ``unclassified``, ``undeclared_reference``,
``inactive_reference``, ``declaration_absent`` and ``declaration_invalid``, with
one ``next_action`` for every state except ``declared``.  No Family is inferred
from the Case text; ``business_family`` is shown as recorded and takes no part
in the resolution.  The declaration only changes ``family`` and the summary
fields ``chains``, ``family_states`` and ``unclassified``; every other row field
is computed exactly as in schema version 1.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from autophone.assets import business_chains
from autophone.assets.business_chains import BusinessChainsDeclaration, load_business_chains
from autophone.assets.promotion_identity import case_registry_snapshot
from autophone.knowledge.preparation_card import NEXT_ACTIONS, build_preparation_card
from autophone.knowledge.query import KnowledgeQueryEngine
from autophone.logging import get_logger
from autophone.paths import APP_ASSET_ROOT

log = get_logger(__name__)

APP_KNOWLEDGE_VIEW_SCHEMA = "autophone.app-knowledge-view/2"

#: ``family.state`` values (proposal §4.7).
DECLARED = "declared"
UNCLASSIFIED = "unclassified"
UNDECLARED_REFERENCE = "undeclared_reference"
INACTIVE_REFERENCE = "inactive_reference"
DECLARATION_ABSENT = "declaration_absent"
DECLARATION_INVALID = "declaration_invalid"
FAMILY_STATES = (
    DECLARED, UNCLASSIFIED, UNDECLARED_REFERENCE, INACTIVE_REFERENCE,
    DECLARATION_ABSENT, DECLARATION_INVALID,
)
FAMILY_NEXT_ACTIONS = {
    UNCLASSIFIED: "为该 Case 选择已声明 Family 或提出 draft Family",
    UNDECLARED_REFERENCE: "修正 family_id 或补 draft 声明条目",
    INACTIVE_REFERENCE: "激活对应声明条目或改为 active Family",
    DECLARATION_ABSENT: "为该 App 起草 business_chains.yaml",
    DECLARATION_INVALID: "修正声明文件",
}
VIEW_UNAVAILABLE = "knowledge_view_unavailable"


def build_app_knowledge_view(
    app: str,
    *,
    statuses: list[str] | None = None,
    case_ids: list[str] | None = None,
    trial_draft_recipes: bool = False,
    root: Path = APP_ASSET_ROOT,
    state_root: Path | None = None,
    run_root: Path | None = None,
) -> dict[str, Any]:
    """Build the view; ``case_ids`` optionally restricts rows to those Case IDs.

    Without ``case_ids`` the output is unchanged.  With it, only matching Cases
    get rows (Case IDs absent from the registry are simply not rows), the
    summary covers those rows and ``filters.case_ids`` records the request.
    Row construction is the same code path either way.
    """
    if not isinstance(trial_draft_recipes, bool):
        raise ValueError("trial_draft_recipes must be a boolean")
    if case_ids is not None and (
        isinstance(case_ids, str) or not all(isinstance(item, str) for item in case_ids)
    ):
        raise ValueError("case_ids must be a list of Case ID strings")
    # One snapshot of the App registries serves every row; the per-Case
    # judgements are still query 1 and the preparation card.
    engine = KnowledgeQueryEngine(
        app, root=root, state_root=state_root, run_root=run_root, snapshot=True)
    profile = engine.catalog.profile
    declaration = load_business_chains(profile)
    if declaration.state == business_chains.STATE_INVALID:
        log.warning(
            "business chain declaration invalid app=%s path=%s errors=%s",
            app, declaration.path, [finding["code"] for finding in declaration.errors],
        )
    wanted = {str(item) for item in statuses} if statuses else None
    wanted_ids = set(case_ids) if case_ids is not None else None
    with case_registry_snapshot():
        cases = [
            case for case in engine.catalog.cases()
            if (wanted is None or str(case.get("status") or "") in wanted)
            and (wanted_ids is None or str(case.get("id")) in wanted_ids)
        ]
        rows = [
            _case_row(engine, case, declaration, trial_draft_recipes=trial_draft_recipes)
            for case in cases
        ]
    view: dict[str, Any] = {
        "schema": APP_KNOWLEDGE_VIEW_SCHEMA,
        "app": app,
        "executes": False,
        "target": {
            "app": profile.app,
            "env": profile.env,
            "build_label": profile.build_label,
        },
        "filters": {"statuses": sorted(wanted) if wanted else None,
                    "trial_draft_recipes": trial_draft_recipes},
        "summary": _summary(rows, declaration, root=root),
        "cases": rows,
    }
    if case_ids is not None:
        # Only present when requested, so the unfiltered view keeps its exact shape.
        view["filters"]["case_ids"] = sorted(wanted_ids)
    if rows and all("error" in row for row in rows):
        # Every selected row failed: the view carries no usable relation or
        # readiness, so it is marked unavailable at App level with the same
        # ``{code, message}`` shape as row errors and the HTTP 422 detail.
        # Rows stay in the body so each failure remains readable.
        view["error"] = {
            "code": VIEW_UNAVAILABLE,
            "message": f"all {len(rows)} selected cases are unavailable; see cases[].error",
        }
    return view


def _family(case: dict[str, Any], declaration: BusinessChainsDeclaration) -> dict[str, Any]:
    """Resolve one Case's Family, Stage and Chain from the declaration; never infer."""
    family_id = str(case.get("family_id") or "").strip() or None
    business_family = str(case.get("business_family") or "").strip() or None
    out: dict[str, Any] = {
        "state": None,
        "family_id": family_id,
        "family_title": None,
        "family_status": None,
        "stage": None,
        "chain": None,
        "business_family": business_family,
        "next_action": None,
    }
    if declaration.state == business_chains.STATE_ABSENT:
        state = DECLARATION_ABSENT
    elif declaration.state != business_chains.STATE_LOADED:
        state = DECLARATION_INVALID
    elif family_id is None:
        state = UNCLASSIFIED
    else:
        family = declaration.family(family_id)
        if family is None:
            state = UNDECLARED_REFERENCE
        else:
            stage = declaration.stage(family.stage_id)
            chain = declaration.chain(family.chain_id)
            out.update({
                "family_title": family.title,
                "family_status": family.status,
                "stage": {"id": stage.id, "title": stage.title, "status": stage.status},
                "chain": {"id": chain.id, "title": chain.title, "status": chain.status},
            })
            state = (
                DECLARED
                if business_chains.family_status_allowed(case.get("status"), family.status)
                else INACTIVE_REFERENCE
            )
    out["state"] = state
    out["next_action"] = FAMILY_NEXT_ACTIONS.get(state)
    return out


def _case_row(
    engine: KnowledgeQueryEngine,
    case: dict[str, Any],
    declaration: BusinessChainsDeclaration,
    *,
    trial_draft_recipes: bool,
) -> dict[str, Any]:
    case_id = str(case.get("id"))
    row: dict[str, Any] = {
        "case_id": case_id,
        "title": case.get("item") or "",
        "status": case.get("status"),
        "automation": case.get("automation") or "",
        "family": _family(case, declaration),
    }
    try:
        relations = engine.query_1(case_id=case_id, trial_draft_recipes=trial_draft_recipes)
        card = build_preparation_card(
            engine.app, case_id, trial_draft_recipes=trial_draft_recipes, engine=engine,
        )
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        # Same asset-error boundary as the preparation card endpoint: one
        # malformed Case, Recipe or declaration must not hide the other rows.
        log.warning(
            "knowledge view row unavailable app=%s case_id=%s: %s",
            engine.app, case_id, exc, exc_info=True,
        )
        return {**row, "error": {"code": VIEW_UNAVAILABLE,
                                 "message": f"{type(exc).__name__}: {exc}"}}
    data = relations["data"]
    checklist = {item["key"]: item for item in card["checklist"]}
    recipe_row = checklist["recipe"]
    oracle_row = checklist["oracle"]
    fixture_row = checklist["fixture_contract"]
    oracle = data["oracle"]
    return {
        **row,
        "recipe": {
            **data["recipe"],
            "state": recipe_row["state"],
            "codes": recipe_row["codes"],
        },
        "operations": data["operation_refs"],
        "fixtures": {
            "declaration_path": data["fixture_declaration"]["path"],
            "declared": card["fixtures"]["declared"],
            "applicable": card["fixtures"]["applicable"],
            "state": fixture_row["state"],
            "codes": fixture_row["codes"],
            "requirements": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "summary": item["summary"],
                    "provider_op": (item.get("provider") or {}).get("op"),
                    "readback_op": (item.get("readback") or {}).get("op"),
                    "reversibility": item.get("reversibility"),
                    "effect": item.get("effect"),
                    "lease": item.get("lease"),
                }
                for item in data["fixture_requirements"]
            ],
            "preconditions": data["preconditions"],
        },
        "oracle": {
            "api_oracle": (
                {"id": oracle.get("id"), "status": oracle.get("status")} if oracle else None
            ),
            "expects": list(case.get("expects") or []),
            "state": oracle_row["state"],
            "codes": oracle_row["codes"],
        },
        "screens": {
            "scope": data["screen_scope"],
            "screen_refs": data["screen_refs"],
            "unresolved_screen_refs": data["unresolved_screen_refs"],
        },
        "preparation": {
            "status": card["status"],
            "ready_for_trial": card["readiness"]["ready_for_trial"],
            "runtime_verified": card["readiness"]["runtime_verified"],
            "blockers": card["blockers"],
            # Carries ``severity``; a non-``blocked`` primary step does not make
            # the row blocked.  Blocked counts below use ``blockers`` only.
            "primary_next_action": card["primary_next_action"],
            # All ordered steps (blocked, attention, unverified rows).
            "next_actions": card["next_actions"],
        },
    }


def _summary(
    rows: list[dict[str, Any]],
    declaration: BusinessChainsDeclaration,
    *,
    root: Path,
) -> dict[str, Any]:
    # Since schema version 2 ``unclassified`` counts every row whose Family
    # identity is not ``declared`` (proposal §4.7).
    unclassified = [row["case_id"] for row in rows if row["family"]["state"] != DECLARED]
    unavailable = [row["case_id"] for row in rows if "error" in row]
    prepared = [row for row in rows if "preparation" in row]
    by_code: dict[str, list[str]] = {}
    for row in prepared:
        for code in row["preparation"]["blockers"]:
            by_code.setdefault(code, []).append(row["case_id"])
    blocked_by_code = [
        {"code": code, "action": NEXT_ACTIONS.get(code), "case_ids": case_ids}
        for code, case_ids in sorted(by_code.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    return {
        "cases_total": len(rows),
        "by_status": dict(sorted(Counter(str(row["status"]) for row in rows).items())),
        "unclassified": {"count": len(unclassified), "case_ids": unclassified},
        "ready_for_trial": sum(1 for row in prepared if row["preparation"]["ready_for_trial"]),
        "runtime_verified": sum(1 for row in prepared if row["preparation"]["runtime_verified"]),
        "blocked": {
            "count": sum(1 for row in prepared if row["preparation"]["blockers"]),
            "by_code": blocked_by_code,
        },
        "unavailable": {"count": len(unavailable), "case_ids": unavailable},
        "chains": _chains_summary(rows, declaration, root=root),
        "family_states": {
            state: sum(1 for row in rows if row["family"]["state"] == state)
            for state in FAMILY_STATES
        },
    }


def _chains_summary(
    rows: list[dict[str, Any]],
    declaration: BusinessChainsDeclaration,
    *,
    root: Path,
) -> dict[str, Any]:
    """Declaration outline with the selected Cases that reference each Family.

    A declared Family without Cases is listed with ``case_ids: []`` as a
    read-only coverage hint.  Cases counted are those whose ``family_id``
    resolves to the Family (``declared`` or ``inactive_reference``).
    """
    members: dict[str, list[str]] = {}
    for row in rows:
        family = row["family"]
        if family["state"] in (DECLARED, INACTIVE_REFERENCE):
            members.setdefault(family["family_id"], []).append(row["case_id"])
    items = []
    for chain in declaration.chains if declaration.loaded else ():
        stages = []
        for stage_id in chain.stage_ids:
            stage = declaration.stages[stage_id]
            stages.append({
                "stage_id": stage.id,
                "title": stage.title,
                "status": stage.status,
                "families": [
                    {
                        "family_id": family_id,
                        "title": declaration.families[family_id].title,
                        "status": declaration.families[family_id].status,
                        "case_ids": members.get(family_id, []),
                    }
                    for family_id in stage.family_ids
                ],
            })
        items.append({
            "chain_id": chain.id,
            "title": chain.title,
            "status": chain.status,
            "stages": stages,
        })
    return {
        "declaration_path": _display_path(declaration.path, root),
        "declaration_sha256": declaration.sha256,
        "state": declaration.state,
        "items": items,
        "errors": list(declaration.errors),
    }


def _display_path(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)
