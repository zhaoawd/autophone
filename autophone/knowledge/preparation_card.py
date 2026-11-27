"""Unified Case preparation card (usecase assetization §18.3, §20).

The card aggregates, for one Case, the static trial checks of knowledge query 2,
the declared business Fixture requirements, the automation lane, effects and
the runtime verification state derived from recorded Draft Trial Runs.

It is a read model.  It never executes a Trial, Setup or Readback and never
writes assets.  ``runtime_verified`` becomes true only when a recorded Run of
the current Case revision, reserved external ID and Recipe digest passed on a
Build whose identity was observed to match, and every declared Fixture has
digest-bound ``ready`` Readback evidence from that same Run.  A draft Recipe is
matched against Draft Trial Runs; a live Recipe against the executed-Recipe
identity ordinary runs record (BL-186).  Both use
``promotion_identity.run_matches_recipe_identity``.  For a live Recipe Case
with a declared Fixture, Readback evidence counts only from a Run submitted for
one of the Case's golden paths (``autophone.assets.golden_paths``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from autophone.assets.build_identity import build_identity_unconfirmed, label_build_mismatch
from autophone.assets.fixture_requirements import (
    declared_fixtures,
    fixture_runtime_states,
    load_readback_evidence,
    load_fixture_requirements,
    validate_case_fixtures,
)
from autophone.contracts import ContractError
from autophone.paths import APP_ASSET_ROOT
from autophone.util import read_json

PREPARATION_CARD_SCHEMA = "autophone.preparation-card/1"

# §20: every gap maps to one direct next action in tester language.
NEXT_ACTIONS: dict[str, str] = {
    "case_not_found": "确认 App 与用例编号",
    "case_not_trial_eligible": "用例生命周期不是 draft 或 active，先恢复或新建草稿",
    "missing_draft_recipe": "为该用例起草 recipes/drafts/<id>.yaml",
    "recipe_missing": "为该用例补 live Recipe，或改走草稿试跑",
    "invalid_recipe": "修正 Recipe：只使用已注册的 Action / Operation 与合法参数",
    "effect_not_allowed": "确认副作用是否应在该 App 允许；否则改写 Recipe",
    "api_oracle_missing": "从原始预期起草 API 断言",
    "api_oracle_not_active": "完成 API 断言的审查与转正",
    "build_label_unset": "在 App Profile 补 build_label",
    "fixture_requirements_undeclared": "在 fixture_requirements.yaml 声明该用例的业务 Fixture",
    "fixture_contract_invalid": "按问题清单修正 Fixture 声明",
    "requires_confirmation": "运行前确认不可逆副作用",
    "blocked_by_infra": "补齐设备或平台能力后再试跑",
    "manual_checkpoint": "配置人工 checkpoint 与续跑说明",
    "no_identity_matching_trial_run": "对当前草稿身份执行显式 Draft Trial",
    "no_identity_matching_live_run": "按当前 live Recipe 与用例 revision 重新运行该用例",
    "latest_trial_not_passed": "查看最近一次试跑报告并处理失败原因",
    "live_recipe_identity_not_recorded": "运行一次该用例以记录 live Recipe 执行身份，或改走显式草稿试跑",
    "installed_build_not_matched": "确认设备安装构建与 App Profile build_label 一致后重跑",
    "fixture_readback_unverified": "在试跑中产出 fixture-readback.json 的独立 Readback 证据",
    "golden_path_run_missing": "live Recipe 用例经黄金路径提交 Run（run --golden-path <路径 id>）以产出 Readback 证据",
    "current_draft_recipe_identity_invalid": "修正用例 revision 或保留编号绑定",
    "current_live_recipe_identity_invalid": "修正用例 revision、保留编号绑定或补齐 live Recipe 文件",
}

# §20 "每种缺口应有直接动作" / "谁能处理" (BL-038 stage B3): every code in
# ``NEXT_ACTIONS`` maps to an action kind and the role that can close it.  The
# dashboard renders a button only for an executable kind; ``guidance`` shows the
# text and the owner.  The kind here is the default; ``next_step`` downgrades
# it to ``guidance`` when the card cannot support the action (see there).
NEXT_ACTION_KINDS: tuple[str, ...] = (
    "draft_recipe",       # open the Case editor at the Recipe drafting entry
    "edit_recipe",        # open the Case editor at the Recipe editing entry
    "start_draft_trial",  # open the trial confirmation and submit an explicit Draft Trial
    "open_trial_report",  # open the run view of the latest identity-matching Run
    "guidance",           # text and owner only, no button
)
NEXT_ACTION_OWNERS: tuple[str, ...] = ("tester", "knowledge_maintainer", "platform_maintainer")
NEXT_ACTION_ROUTES: dict[str, tuple[str, str]] = {
    "case_not_found": ("guidance", "tester"),
    "case_not_trial_eligible": ("guidance", "tester"),
    "missing_draft_recipe": ("draft_recipe", "tester"),
    "recipe_missing": ("edit_recipe", "tester"),
    "invalid_recipe": ("edit_recipe", "tester"),
    # Allowing an effect is an App Profile policy decision before any Recipe edit.
    "effect_not_allowed": ("guidance", "knowledge_maintainer"),
    "api_oracle_missing": ("guidance", "knowledge_maintainer"),
    "api_oracle_not_active": ("guidance", "knowledge_maintainer"),
    "build_label_unset": ("guidance", "platform_maintainer"),
    "fixture_requirements_undeclared": ("guidance", "knowledge_maintainer"),
    "fixture_contract_invalid": ("guidance", "knowledge_maintainer"),
    "requires_confirmation": ("guidance", "tester"),
    "blocked_by_infra": ("guidance", "platform_maintainer"),
    "manual_checkpoint": ("guidance", "knowledge_maintainer"),
    "no_identity_matching_trial_run": ("start_draft_trial", "tester"),
    "no_identity_matching_live_run": ("guidance", "tester"),
    "latest_trial_not_passed": ("open_trial_report", "tester"),
    "live_recipe_identity_not_recorded": ("guidance", "tester"),
    "installed_build_not_matched": ("guidance", "platform_maintainer"),
    "fixture_readback_unverified": ("guidance", "knowledge_maintainer"),
    "golden_path_run_missing": ("guidance", "tester"),
    "current_draft_recipe_identity_invalid": ("guidance", "knowledge_maintainer"),
    "current_live_recipe_identity_invalid": ("guidance", "knowledge_maintainer"),
}


def next_step(code: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Structured next step for one mapped code: ``code``, ``action``, ``kind``, ``owner``.

    Without ``context`` the kind is the table default.  With the card context
    the kind is the one the card supports:

    - ``start_draft_trial`` needs ``ready_for_trial`` and a draft Recipe source;
      the step then carries ``trial_selector``, the explicit Draft Trial
      selector for the Case (``statuses: [draft]`` for a draft Case,
      ``trial_draft_recipes: true`` otherwise, as the case-drafts list).
    - ``open_trial_report`` needs the run ID of the evidence Run; the step then
      carries ``run_id``.

    Otherwise the kind becomes ``guidance``; the run ID is never guessed.
    """
    kind, owner = NEXT_ACTION_ROUTES[code]
    step: dict[str, Any] = {"code": code, "action": NEXT_ACTIONS[code], "kind": kind, "owner": owner}
    if context is None:
        return step
    if kind == "start_draft_trial":
        if context.get("ready_for_trial") is True and context.get("recipe_source") == "draft":
            step["trial_selector"] = dict(context["trial_selector"])
        else:
            step["kind"] = "guidance"
    elif kind == "open_trial_report":
        run_id = context.get("evidence_run_id")
        if run_id:
            step["run_id"] = str(run_id)
        else:
            step["kind"] = "guidance"
    return step


# Severity order for ``primary_next_action``: a ``blocked`` row always wins; an
# ``attention`` or ``unverified`` row is chosen only when nothing blocks.
PRIMARY_SEVERITIES: tuple[str, ...] = ("blocked", "attention", "unverified")

# Run-time notices (2026-09-25 ruling): an ``attention`` code that is a fixed
# property of the Case, confirmed or handled before every run, not a gap that
# can be closed.  They stay in ``next_actions`` and in their checklist row but
# never become ``primary_next_action``.  Fixable ``attention`` codes (e.g.
# ``build_label_unset``) are selected in the normal order.
RUNTIME_NOTICE_CODES: frozenset[str] = frozenset({"requires_confirmation", "manual_checkpoint"})

# Codes the trial run row carries while it is unverified.
TRIAL_RUN_CODES: frozenset[str] = frozenset({
    "live_recipe_identity_not_recorded", "current_draft_recipe_identity_invalid",
    "current_live_recipe_identity_invalid", "no_identity_matching_live_run",
    "no_identity_matching_trial_run", "latest_trial_not_passed",
})


def primary_next_action(
    checklist: list[dict[str, Any]], context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The single next step of a card, or ``None`` when no row needs one.

    Rows are searched by severity: ``blocked`` rows first, then ``attention``
    rows, then ``unverified`` rows.  Within one severity the priority is the
    checklist order (lifecycle, target, automation, recipe, effect, oracle,
    fixture contract, trial run, installed build, readback) and, within a row,
    the order of its codes; the first code with a mapped action that is not a
    run-time notice (``RUNTIME_NOTICE_CODES``) is chosen.  The same order drives
    ``blockers`` and ``next_actions``.  ``severity`` names the row state the
    step came from; only ``severity == "blocked"`` means the card is blocked
    (``blockers`` stays the source of truth for that).  When only run-time
    notices remain the result is ``None``.  ``kind`` and ``owner`` (and the
    optional ``trial_selector`` / ``run_id``) come from ``next_step`` with the
    given card ``context``.
    """
    for severity in PRIMARY_SEVERITIES:
        for row in checklist:
            if row.get("state") != severity:
                continue
            for code in row.get("codes") or []:
                if code in NEXT_ACTIONS and code not in RUNTIME_NOTICE_CODES:
                    step = next_step(code, context)
                    return {"code": code, "checklist_key": row.get("key"),
                            "action": step.pop("action"), "severity": severity,
                            **{key: value for key, value in step.items() if key != "code"}}
    return None


def _row(key: str, label: str, state: str, detail: str = "", codes: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "state": state, "detail": detail, "codes": codes or []}


def build_preparation_card(
    app: str,
    case_id: str,
    *,
    trial_draft_recipes: bool = False,
    root: Path = APP_ASSET_ROOT,
    state_root: Path | None = None,
    run_root: Path | None = None,
    engine: Any = None,
) -> dict[str, Any]:
    """Build one card.  ``engine`` reuses a query engine of the same App (the
    App knowledge view shares one snapshot engine); ``root``, ``state_root``
    and ``run_root`` are then taken from it."""
    from autophone.knowledge.query import KnowledgeQueryEngine

    if not isinstance(trial_draft_recipes, bool):
        raise ValueError("trial_draft_recipes must be a boolean")
    if engine is None:
        engine = KnowledgeQueryEngine(app, root=root, state_root=state_root, run_root=run_root)
    elif engine.app != app:
        raise ValueError(f"engine app {engine.app!r} does not match {app!r}")
    profile = engine.catalog.profile
    case = engine.catalog.case(case_id)
    base = {
        "schema": PREPARATION_CARD_SCHEMA,
        "app": app,
        "case_id": case_id,
        "executes": False,
    }
    if case is None:
        step = next_step("case_not_found")
        return {**base, "status": "case_not_found", "blockers": ["case_not_found"],
                "next_actions": [NEXT_ACTIONS["case_not_found"]],
                "next_steps": [step],
                "primary_next_action": {"code": "case_not_found", "checklist_key": None,
                                        "action": NEXT_ACTIONS["case_not_found"],
                                        "severity": "blocked",
                                        "kind": step["kind"], "owner": step["owner"]}}

    from autophone.knowledge.trial_confirmation import build_trial_context

    context = build_trial_context(engine, case_id, trial_draft_recipes=trial_draft_recipes)
    case = context["case"]
    static = context["static"]
    static_codes = [str(gap.get("code")) for gap in static["requirements"]]
    automation = str(case.get("automation") or "")

    # Fixture contract.
    document = context["document"]
    rows = document["cases"].get(case_id.upper(), [])
    requirements = context["requirements"]
    issues = [dict(issue) for issue in document["issues"]
              if issue.get("case_id") in {"", case_id.upper()}]
    issues += [issue.to_dict() for issue in validate_case_fixtures(
        profile, case, rows, schema=document["schema"])]
    declared = bool(rows)
    if issues:
        fixture_state, fixture_codes = "blocked", ["fixture_contract_invalid"]
    elif declared:
        fixture_state, fixture_codes = "pass", []
    elif automation == "needs_fixture":
        fixture_state, fixture_codes = "blocked", ["fixture_requirements_undeclared"]
    else:
        fixture_state, fixture_codes = "not_applicable", []
    fixture_contract_ready = fixture_state in {"pass", "not_applicable"}
    fixtures_applicable = declared or automation == "needs_fixture"

    # Effects that need an explicit confirmation before a Trial.
    irreversible = [
        {"fixture_id": req["id"], "effect": req["effect"]}
        for req in requirements if req["reversibility"] == "irreversible"
    ]

    runtime = _runtime_verification(
        engine, profile, case_id,
        recipe_source=static["recipe_source"],
        requirements=requirements,
        fixtures_applicable=fixtures_applicable,
    )

    rows_out = _checklist(
        profile=profile, case=case, static_codes=static_codes, static=static,
        fixture_state=fixture_state, fixture_codes=fixture_codes,
        irreversible=irreversible, runtime=runtime, automation=automation,
        fixtures_applicable=fixtures_applicable,
    )
    # Declared first-trial preparation (display only, not digest-bound) is shown
    # while the Readback is still unverified, e.g. a provider rehearsal that an
    # App needs after a fresh install before the Rig readiness probe can pass.
    preparation = list(dict.fromkeys(
        note for req in requirements for note in req.get("trial_preparation") or []
    ))
    for row in rows_out:
        # Per-row direct next step (§20), so a reader does not have to map codes.
        row["next_actions"] = [NEXT_ACTIONS[code] for code in row["codes"] if code in NEXT_ACTIONS]
        if row["key"] == "fixture_readback" and row["state"] == "unverified":
            row["next_actions"] += preparation
    blockers = [code for row in rows_out if row["state"] == "blocked" for code in row["codes"]]
    attention = [code for row in rows_out if row["state"] == "attention" for code in row["codes"]]
    unverified = [code for row in rows_out if row["state"] == "unverified" for code in row["codes"]]
    ready_for_trial = bool(static["trial_ready"]) and fixture_contract_ready and not blockers
    step_context = {
        "ready_for_trial": ready_for_trial,
        "recipe_source": static["recipe_source"],
        "trial_selector": (
            {"ids": [case_id], "statuses": ["draft"]} if case.get("status") == "draft"
            else {"ids": [case_id], "trial_draft_recipes": True}
        ),
        "evidence_run_id": (runtime.get("evidence_run") or {}).get("run_id"),
    }
    for row in rows_out:
        # Structured form of the row's code steps (B3); ``next_actions`` keeps
        # the text, including the declared trial preparation notes.
        row["next_steps"] = [next_step(code, step_context)
                             for code in row["codes"] if code in NEXT_ACTIONS]
    ordered_codes = [code for code in dict.fromkeys([*blockers, *attention, *unverified])
                     if code in NEXT_ACTIONS]
    actions = [NEXT_ACTIONS[code] for code in ordered_codes]
    if "fixture_readback_unverified" in unverified:
        actions += [note for note in preparation if note not in actions]
    if blockers:
        status = "blocked"
    elif runtime["runtime_verified"]:
        status = "runtime_verified"
    else:
        status = "ready_for_trial"
    return {
        **base,
        "status": status,
        "case": {
            "id": case.get("id"),
            "item": case.get("item") or "",
            "status": case.get("status"),
            "automation": automation,
            "revision": case.get("revision"),
            "external_id": ((case.get("raw") or {}).get("external_id") or None),
        },
        "target": {
            "app": profile.app,
            "bundle_id": profile.bundle_id,
            "env": profile.env,
            "build_label": profile.build_label,
        },
        "readiness": {
            "static_trial_ready": bool(static["trial_ready"]),
            "fixture_contract_ready": fixture_contract_ready,
            "ready_for_trial": ready_for_trial,
            "runtime_verified": runtime["runtime_verified"],
            "readiness_scope": "runtime" if runtime["runtime_verified"] else "static",
        },
        "static": static,
        "fixtures": {
            "declaration_path": document["path"],
            "declared": declared,
            "applicable": fixtures_applicable,
            "requirements": requirements,
            "issues": issues,
            "runtime": runtime["fixtures"],
        },
        "effects": context["effects"],
        "trial_confirmation": context["trial_confirmation"],
        "runtime": {key: value for key, value in runtime.items() if key != "fixtures"},
        "checklist": rows_out,
        "blockers": list(dict.fromkeys(blockers)),
        "next_actions": actions,
        "next_steps": [next_step(code, step_context) for code in ordered_codes],
        "primary_next_action": primary_next_action(rows_out, step_context),
    }


def _runtime_verification(
    engine: Any,
    profile: Any,
    case_id: str,
    *,
    recipe_source: str,
    requirements: list[dict[str, Any]],
    fixtures_applicable: bool,
) -> dict[str, Any]:
    from autophone.application.run_index import PromotionSubject, RunCatalog
    from autophone.assets.promotion_identity import (
        RECIPE_SOURCE_DRAFT,
        RECIPE_SOURCE_LIVE,
        current_recipe_identity,
        recorded_recipe_identity,
        run_matches_recipe_identity,
    )

    verified: list[str] = []
    reasons: list[str] = []
    fixture_states = [
        {"fixture_id": req["id"], "state": "unverified", "reason": "no_runtime_evidence"}
        for req in requirements
    ]
    result: dict[str, Any] = {
        "runtime_verified": False,
        "evidence_run": None,
        "matching_trial_runs": 0,
        "identity_rejected_runs": 0,
        "verified": verified,
        "unverified": [],
        "reasons": reasons,
        "fixtures": fixture_states,
    }
    checks = ["device", "installed_build"]
    if fixtures_applicable:
        checks += ["fixture_state", "readback"]

    def finish() -> dict[str, Any]:
        result["unverified"] = [name for name in checks if name not in verified]
        return result

    live = recipe_source != RECIPE_SOURCE_DRAFT
    source = RECIPE_SOURCE_LIVE if live else RECIPE_SOURCE_DRAFT
    result["recipe_source"] = source
    try:
        identity = current_recipe_identity(profile, case_id, source=source)
    except ContractError as exc:
        reasons.append(
            "current_live_recipe_identity_invalid" if live
            else "current_draft_recipe_identity_invalid"
        )
        result["detail"] = str(exc)
        return finish()
    result["current_recipe_identity"] = identity
    catalog = RunCatalog(engine.run_root)
    if live:
        # A live Recipe runs in ordinary regression runs; BL-186 records the
        # identity it executed there.  Draft Trials never select a live Recipe.
        runs = catalog.case_recipe_runs(app=profile.app, case_id=case_id)
    else:
        runs = catalog.promotion_runs(
            PromotionSubject(kind="case", asset_id=case_id, app=profile.app)
        )
    matching = [run for run in runs if run_matches_recipe_identity(run, identity)]
    result["matching_trial_runs"] = len(matching)
    result["identity_rejected_runs"] = len(runs) - len(matching)
    if not matching:
        if live and identity is None:
            reasons.append("current_live_recipe_identity_invalid")
            result["detail"] = "live recipe file is missing"
        elif live and not any(
            (recorded_recipe_identity(run) or {}).get("source") == RECIPE_SOURCE_LIVE
            for run in runs
        ):
            reasons.append("live_recipe_identity_not_recorded")
        elif live:
            reasons.append("no_identity_matching_live_run")
        else:
            reasons.append("no_identity_matching_trial_run")
        return finish()
    # A live Recipe Case with a declared Fixture gets Readback evidence only from
    # a Run submitted for one of its golden paths (run-spec.json golden_path
    # marker matching the current declaration).  Such Runs are the evidence
    # candidates when any exist; ordinary regression Runs never bind a Readback
    # plan, so their evidence (if any file exists) is not accepted.
    golden_marked = False
    if live and fixtures_applicable:
        from autophone.assets.golden_paths import marked_for_case

        marked = [
            run for run in matching
            if marked_for_case(
                profile, read_json(Path(str(run["run_dir"])) / "run-spec.json"), case_id)
        ]
        result["golden_path_runs"] = len(marked)
        if marked:
            matching, golden_marked = marked, True
    latest = matching[-1]
    run_dir = Path(str(latest["run_dir"]))
    platform = read_json(run_dir / "platform-run-result.json") or {}
    package = platform.get("package") if isinstance(platform.get("package"), dict) else {}
    result["evidence_run"] = {
        "run_id": latest.get("run_id"),
        "run_dir": str(run_dir),
        "ended_at": latest.get("ended_at"),
        "status": latest.get("status"),
        "review_status": latest.get("review_status"),
        "build_identity_match": package.get("build_identity_match"),
        "profile_build_label": package.get("profile_build_label"),
        "comparison_build_id": package.get("comparison_build_id"),
        "golden_path_run": golden_marked,
    }
    if latest.get("status") != "passed":
        reasons.append("latest_trial_not_passed")
        return finish()
    verified.append("device")
    if (
        # match at an accepted granularity: build_marker, or info_plist only
        # for a source_revision.kind=unknown Build (BL-241).
        build_identity_unconfirmed(package) is None
        and profile.build_label is not None
        and package.get("profile_build_label") == profile.build_label
        # The compared Build must be the one the label names (BL-190).
        and label_build_mismatch(profile.build_label, package) is None
    ):
        verified.append("installed_build")
    else:
        reasons.append("installed_build_not_matched")
    if fixtures_applicable:
        if not requirements:
            reasons.append("fixture_requirements_undeclared")
        elif live and not golden_marked:
            result["fixtures"] = [
                {"fixture_id": req["id"], "state": "unverified", "reason": "golden_path_run_missing"}
                for req in requirements
            ]
            reasons += ["fixture_readback_unverified", "golden_path_run_missing"]
        else:
            evidence, manifest_check = load_readback_evidence(run_dir)
            result["evidence_manifest"] = manifest_check
            states = fixture_runtime_states(
                requirements, evidence, case_id=case_id,
                run_id=str(latest.get("run_id") or run_dir.name),
            )
            result["fixtures"] = states
            if all(state["state"] == "ready" for state in states):
                verified += ["fixture_state", "readback"]
            else:
                reasons.append("fixture_readback_unverified")
    result["runtime_verified"] = all(name in verified for name in checks)
    return finish()


def _checklist(
    *,
    profile: Any,
    case: dict[str, Any],
    static_codes: list[str],
    static: dict[str, Any],
    fixture_state: str,
    fixture_codes: list[str],
    irreversible: list[dict[str, Any]],
    runtime: dict[str, Any],
    automation: str,
    fixtures_applicable: bool,
) -> list[dict[str, Any]]:
    by_kind: dict[str, list[str]] = {}
    for gap in static["requirements"]:
        by_kind.setdefault(str(gap.get("kind")), []).append(str(gap.get("code")))

    def static_row(key: str, label: str, kind: str) -> dict[str, Any]:
        codes = by_kind.get(kind, [])
        return _row(key, label, "blocked" if codes else "pass", "; ".join(codes), codes)

    rows = [static_row("lifecycle", "用例生命周期", "lifecycle")]
    rows.append(
        _row("target", "App / 环境 / Build", "pass",
             f"{profile.app} / {profile.env or '-'} / {profile.build_label}")
        if profile.build_label
        else _row("target", "App / 环境 / Build", "attention", "build_label 未设置",
                  ["build_label_unset"])
    )
    if automation == "blocked_by_infra":
        rows.append(_row("automation", "自动化档位", "blocked", automation, ["blocked_by_infra"]))
    elif automation == "manual_checkpoint":
        rows.append(_row("automation", "自动化档位", "attention", automation, ["manual_checkpoint"]))
    else:
        rows.append(_row("automation", "自动化档位", "pass", automation or "-"))
    rows.append(static_row("recipe", "执行方案（Recipe / Operation）", "capability"))
    effect_codes = by_kind.get("effect", [])
    if effect_codes:
        rows.append(_row("effect", "副作用", "blocked", "; ".join(effect_codes), effect_codes))
    elif irreversible:
        rows.append(_row("effect", "副作用", "attention",
                         "不可逆：" + ", ".join(f"{item['fixture_id']}({item['effect']})"
                                            for item in irreversible),
                         ["requires_confirmation"]))
    else:
        rows.append(_row("effect", "副作用", "pass"))
    rows.append(static_row("oracle", "断言依据（Oracle）", "evidence"))
    rows.append(_row("fixture_contract", "业务 Fixture 声明", fixture_state,
                     "; ".join(fixture_codes), fixture_codes))
    reasons = runtime["reasons"]
    trial_codes = [code for code in reasons if code in TRIAL_RUN_CODES]
    rows.append(_row("trial_run", "真机试跑", "pass" if "device" in runtime["verified"] else "unverified",
                     "; ".join(trial_codes), trial_codes))
    rows.append(_row("installed_build", "已安装构建", "pass"
                     if "installed_build" in runtime["verified"] else "unverified",
                     "", [code for code in reasons if code == "installed_build_not_matched"]))
    if fixtures_applicable:
        readback_ok = "readback" in runtime["verified"]
        readback_codes = [] if readback_ok else ["fixture_readback_unverified"] + [
            code for code in reasons if code == "golden_path_run_missing"]
        rows.append(_row("fixture_readback", "Fixture 独立 Readback",
                         "pass" if readback_ok else "unverified", "", readback_codes))
    else:
        rows.append(_row("fixture_readback", "Fixture 独立 Readback", "not_applicable"))
    return rows
