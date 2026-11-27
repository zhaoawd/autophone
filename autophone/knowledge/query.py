"""The eight frozen App knowledge queries with structured no-answer states.

Run evidence is read through application.run_index; knowledge claims and their
evidence levels remain owned by this package.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.application import run_index
from autophone.assets.fixture_requirements import declared_fixtures, load_fixture_requirements
from autophone.assets.promotion_identity import (
    RECIPE_SOURCE_DRAFT, RECIPE_SOURCE_LIVE, current_draft_recipe_identity,
    current_recipe_identity, recipe_path, recipe_relative_path,
)
from autophone.contracts import ContractError
from autophone.execution.app_recipes import import_app_recipes
from autophone.execution.effects import (
    EffectPolicyError, case_operation_effects, recipe_step_effects,
)
from autophone.execution.engine.recipe import (
    RecipeError, load_recipe_document, operation_scope, operations_for_app,
    validate_recipe_for_app,
)
from autophone.knowledge.catalog import KnowledgeCatalog
from autophone.knowledge.freshness import evidence_state
from autophone.util import read_json


def _answer(number: int, data: Any, *, gaps: list[str] | None = None) -> dict[str, Any]:
    return {
        "query": number,
        "status": "answer",
        "data": data,
        "gaps": gaps or [],
    }


def _no(number: int, status: str, **data: Any) -> dict[str, Any]:
    return {"query": number, "status": status, "data": data, "gaps": [status]}


def _operation_names(steps: list[Any]) -> list[str]:
    names: list[str] = []
    for step in steps:
        if isinstance(step, dict) and step.get("operation"):
            name = str(step["operation"])
            if name not in names:
                names.append(name)
    return names


def _result_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    plane_changes = []
    for plane in sorted(set(left.get("planes") or {}) | set(right.get("planes") or {})):
        a = (left.get("planes") or {}).get(plane) or {}
        b = (right.get("planes") or {}).get(plane) or {}
        if (a.get("verdict"), a.get("detail")) != (b.get("verdict"), b.get("detail")):
            plane_changes.append({"plane": plane, "baseline": a, "target": b})
    left_cases = {str(row.get("id")): row for row in left.get("cases") or []}
    right_cases = {str(row.get("id")): row for row in right.get("cases") or []}
    case_changes = [
        {
            "case_id": case_id,
            "baseline_status": (left_cases.get(case_id) or {}).get("status"),
            "target_status": (right_cases.get(case_id) or {}).get("status"),
        }
        for case_id in sorted(set(left_cases) | set(right_cases))
        if (left_cases.get(case_id) or {}).get("status") != (right_cases.get(case_id) or {}).get("status")
    ]
    return {
        "status": {"baseline": left.get("status"), "target": right.get("status")},
        "plane_changes": plane_changes,
        "case_changes": case_changes,
    }


def _operation_refs(
    catalog: KnowledgeCatalog,
    recipe_ops: list[str],
    case: dict[str, Any],
    fixtures: list[dict[str, Any]],
    registered: set[str] | None,
) -> list[dict[str, Any]]:
    """Operations a Case names directly, each with the role that names it.

    Roles: Recipe step, Case setup / teardown / post-case cleanup, and the
    declared Fixture provider / Readback.  ``status`` is the asset status in
    operations.yaml; a name without an asset is ``code_registered`` when the
    App's executor registry has it, ``missing`` when it does not, and
    ``unknown`` when the registry could not be loaded.
    """
    named: list[tuple[str, str]] = [("recipe", name) for name in recipe_ops]
    for role in ("setup", "teardown", "post_case_cleanup"):
        for item in case.get(role) or []:
            if isinstance(item, dict) and item.get("op"):
                named.append((role, str(item["op"])))
    for fixture in fixtures:
        for role in ("provider", "readback"):
            ref = fixture.get(role)
            if isinstance(ref, dict) and ref.get("op"):
                named.append((f"fixture_{role}", str(ref["op"])))
    out: list[dict[str, Any]] = []
    for role, name in dict.fromkeys(named):
        asset = catalog.operation(name)
        in_registry = None if registered is None else name in registered
        if asset:
            status = str(asset.get("status") or "unknown")
        elif in_registry is None:
            status = "unknown"
        else:
            status = "code_registered" if in_registry else "missing"
        out.append({
            "name": name,
            "role": role,
            "id": asset.get("id") if asset else None,
            "status": status,
            "registered": in_registry,
        })
    return out


def _operation_screen_refs(operation: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    expectations = operation.get("screen_expectations")
    if isinstance(expectations, dict):
        refs += [(str(value), "screen_expectations") for value in expectations.values()]
    if operation.get("screen_starts_with"):
        refs.append((str(operation["screen_starts_with"]), "screen_starts_with"))
    navigation = operation.get("navigation")
    if isinstance(navigation, dict):
        refs += [
            (str(navigation[key]), f"navigation.{key}")
            for key in ("from", "to") if navigation.get(key)
        ]
    return refs


def _case_screens(
    catalog: KnowledgeCatalog,
    recipe: dict[str, Any],
    operation_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Screens related to a Case through declared asset references only.

    Sources: the Recipe's ``screen_expectations`` / ``screen_starts_with``
    sidecar, and each referenced Operation's ``screen_expectations``,
    ``screen_starts_with`` and ``navigation.from`` / ``navigation.to``,
    following nested ``operation`` steps.  Without any such reference the
    relation is unknown: every active screen is returned and ``screen_scope``
    says so; screens are never matched by text.
    """
    refs: dict[str, list[str]] = {}

    def add(screen_id: str, source: str) -> None:
        sources = refs.setdefault(screen_id, [])
        if source not in sources:
            sources.append(source)

    for screen_id, source in _operation_screen_refs(
        {key: recipe.get(key) for key in ("screen_expectations", "screen_starts_with")}
    ):
        add(screen_id, f"recipe.{source}")
    pending = [str(ref["name"]) for ref in operation_refs]
    visited: set[str] = set()
    while pending:
        name = pending.pop(0)
        if name in visited:
            continue
        visited.add(name)
        operation = catalog.operation(name)
        if operation is None:
            continue
        for screen_id, source in _operation_screen_refs(operation):
            add(screen_id, f"operation:{name}.{source}")
        pending += _operation_names(list(operation.get("steps") or []))
    screens = catalog.screens()
    if not refs:
        return {
            "screen_scope": "unresolved_all_active",
            "screen_refs": [],
            "unresolved_screen_refs": [],
            "screens": [row for row in screens if row.get("status") == "active"],
        }
    known = {str(row.get("id")) for row in screens}
    return {
        "screen_scope": "referenced",
        "screen_refs": [{"screen_id": key, "sources": value} for key, value in refs.items()],
        "unresolved_screen_refs": [key for key in refs if key not in known],
        "screens": [row for row in screens if str(row.get("id")) in refs],
    }


class KnowledgeQueryEngine:
    def __init__(
        self,
        app: str,
        *,
        root: Path = APP_ASSET_ROOT,
        state_root: Path | None = None,
        run_root: Path | None = None,
        snapshot: bool = False,
    ) -> None:
        self.catalog = KnowledgeCatalog(app, root=root, state_root=state_root, snapshot=snapshot)
        self.app = app
        self.root = root
        self.run_root = run_root or (
            self.catalog.profile.host_state_root / "runs" / "platform"
        )

    def execute(self, number: int, **params: Any) -> dict[str, Any]:
        method = getattr(self, f"query_{number}", None)
        if method is None:
            return _no(number, "unsupported_query")
        required = {
            1: "case_id",
            2: "case_id",
            3: "screen_id",
            4: "operation",
            5: "endpoint",
            6: "run_id",
            8: "slot",
        }.get(number)
        if required and not str(params.get(required) or "").strip():
            return _no(
                number,
                "missing_query_parameter",
                parameter=required,
            )
        return method(**params)

    def query_1(
        self,
        *,
        case_id: str,
        current_subject_build_key: str | None = None,
        trial_draft_recipes: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        case = self.catalog.case(case_id)
        if case is None:
            return _no(1, "case_not_found", case_id=case_id)
        if not isinstance(trial_draft_recipes, bool):
            return _no(1, "invalid_query_parameter", parameter="trial_draft_recipes")
        # Same Recipe selection as query 2: a draft Case (or an explicit draft
        # trial) reads recipes/drafts/ and never falls back to the live Recipe.
        draft = case.get("status") == "draft" or trial_draft_recipes
        recipe_ref = self._recipe_ref(case_id, draft=draft)
        gaps: list[str] = []
        try:
            recipe = self.catalog.recipe(case_id, draft=draft)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            # Same judgement as query 2: an unparsable Recipe is an
            # ``invalid_recipe`` gap of this Case with no steps, not a query failure.
            recipe = None
            recipe_ref["parse_error"] = f"{type(exc).__name__}: {exc}"
            gaps.append("invalid_recipe")
            steps: list[Any] = []
        else:
            steps = list((recipe or {}).get("steps") or case.get("steps") or [])
        op_names = _operation_names(steps)
        operations = [self.catalog.operation(name) for name in op_names]
        operations = [row for row in operations if row is not None]
        fixture_doc = load_fixture_requirements(self.catalog.profile)
        fixtures = declared_fixtures(fixture_doc["cases"].get(case_id.upper(), []))
        operation_refs = _operation_refs(
            self.catalog, op_names, case, fixtures, self._registered_operations())
        screen_view = _case_screens(self.catalog, recipe or {}, operation_refs)
        oracle = self.catalog.api_oracle(case_id)
        if recipe is None and "invalid_recipe" not in gaps:
            gaps.append("missing_draft_recipe" if draft else "recipe_missing")
        if any(ref["status"] == "missing" for ref in operation_refs):
            gaps.append("operation_missing")
        if screen_view["unresolved_screen_refs"]:
            gaps.append("screen_ref_not_found")
        if oracle is None:
            gaps.append("oracle_missing")
        elif oracle.get("status") != "active":
            gaps.append("oracle_not_active")
        return _answer(1, {
            "case": case,
            "build_identity": current_subject_build_key or "unconfirmed",
            "recipe": recipe_ref,
            "operations": operations,
            "operation_refs": operation_refs,
            # Declared business Fixtures (fixture_requirements.yaml); the Case's
            # natural-language precondition text stays in ``preconditions``.
            "fixture_requirements": fixtures,
            "fixture_declaration": {
                "path": fixture_doc["path"],
                "present": fixture_doc["present"],
                "declared": bool(fixtures),
                "issues": [
                    issue for issue in fixture_doc["issues"]
                    if issue.get("case_id") in {"", case_id.upper()}
                ],
            },
            "preconditions": case.get("precondition") or [],
            **screen_view,
            "oracle": oracle,
        }, gaps=gaps)

    def _registered_operations(self) -> set[str] | None:
        """Operation names in this App's executor registry; ``None`` if it cannot load.

        Loading imports the App's recipes package, exactly as query 2 does
        before its static Recipe validation; nothing is executed.
        """
        try:
            import_app_recipes(self.app, root=self.root)
            return set(operations_for_app(self.app, root=self.root))
        except (OSError, ValueError, TypeError, ImportError, SyntaxError, yaml.YAMLError):
            return None

    def _recipe_ref(self, case_id: str, *, draft: bool) -> dict[str, Any]:
        """Path and executed-Recipe identity of the selected Recipe (read only)."""
        source = RECIPE_SOURCE_DRAFT if draft else RECIPE_SOURCE_LIVE
        profile = self.catalog.profile
        path = recipe_path(profile, case_id, source=source)
        ref: dict[str, Any] = {
            "source": source,
            "path": recipe_relative_path(profile, path) or str(path),
            "present": path.is_file(),
            "identity": None,
        }
        try:
            ref["identity"] = current_recipe_identity(profile, case_id, source=source)
        except ContractError as exc:
            ref["identity_error"] = str(exc)
        return ref

    def query_2(
        self, *, case_id: str, trial_draft_recipes: bool = False, **_: Any,
    ) -> dict[str, Any]:
        """Static prerequisites only; this query never performs a trial Run."""
        case = self.catalog.case(case_id)
        if case is None:
            return _no(2, "case_not_found", case_id=case_id)
        if not isinstance(trial_draft_recipes, bool):
            return _no(2, "invalid_query_parameter", parameter="trial_draft_recipes")
        gaps: list[dict[str, Any]] = []
        if case.get("status") not in {"draft", "active"}:
            gaps.append({"kind": "lifecycle", "code": "case_not_trial_eligible"})
        # Match explicit trial selection: a missing draft never falls back to a
        # live Recipe or the process-global compatibility registry.
        draft = case.get("status") == "draft" or trial_draft_recipes
        recipe_dir = self.catalog.profile.recipes_dir()
        if draft:
            recipe_dir /= "drafts"
        recipe_path = recipe_dir / f"{case_id.lower()}.yaml"
        # Per-step effects of the selected Recipe; None when it cannot be resolved.
        recipe_effects: list[dict[str, Any]] | None = None
        if not recipe_path.is_file():
            gaps.append({
                "kind": "capability",
                "code": "missing_draft_recipe" if draft else "recipe_missing",
            })
        else:
            try:
                if draft and current_draft_recipe_identity(self.catalog.profile, case_id) is None:
                    raise RecipeError("selected draft recipe is no longer available")
                document = load_recipe_document(recipe_path)
                if str(document.get("id") or "").upper() != case_id.upper():
                    raise RecipeError("recipe id does not match selected case")
                import_app_recipes(self.app, root=self.root)
                with operation_scope(self.app, root=self.root):
                    try:
                        validate_recipe_for_app(document["steps"], self.catalog.profile)
                    except EffectPolicyError:
                        # Shape is valid; only the effect policy rejected it.
                        recipe_effects = recipe_step_effects(document["steps"])
                        raise
                    recipe_effects = recipe_step_effects(document["steps"])
            except EffectPolicyError as exc:
                gaps.append({"kind": "effect", "code": "effect_not_allowed", "detail": str(exc)})
            except (OSError, ValueError, TypeError, ImportError, SyntaxError, yaml.YAMLError) as exc:
                gaps.append({"kind": "capability", "code": "invalid_recipe", "detail": str(exc)})
        oracle = self.catalog.api_oracle(case_id)
        case_operation_rows = self._case_operation_effects(case)
        if "API" in "\n".join(str(item) for item in case.get("expects") or []):
            if not oracle:
                gaps.append({"kind": "evidence", "code": "api_oracle_missing"})
            elif oracle.get("status") != "active":
                gaps.append({"kind": "evidence", "code": "api_oracle_not_active"})
        return _answer(2, {
            "case_id": case_id,
            "trial_ready": not gaps,
            "readiness_scope": "static",
            "recipe_source": "draft" if draft else "live",
            "runtime_verified": False,
            "unverified": ["device", "installed_build", "fixture_state", "readback"],
            "requirements": gaps,
            "recipe_effects": recipe_effects,
            # Case setup / teardown / post-case cleanup Operations a Trial also
            # dispatches; None when the App's executor registry cannot load.
            "case_operation_effects": case_operation_rows,
        })

    def _case_operation_effects(self, case: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Effect decisions of the Case's own Operation refs (read only, never dispatched)."""
        try:
            import_app_recipes(self.app, root=self.root)
            with operation_scope(self.app, root=self.root):
                return case_operation_effects(case)
        except (OSError, ValueError, TypeError, ImportError, SyntaxError, yaml.YAMLError):
            return None

    def query_3(self, *, screen_id: str, **_: Any) -> dict[str, Any]:
        screen = self.catalog.screen(screen_id)
        if screen is None:
            return _no(3, "screen_not_found", screen_id=screen_id)
        tokens = {
            str(item.get("text"))
            for item in screen.get("must") or []
            if isinstance(item, dict) and item.get("text")
        }
        operations = []
        for operation in self.catalog.operations():
            serialized = json.dumps(operation.get("steps") or [], ensure_ascii=False)
            if any(token in serialized for token in tokens):
                operations.append(operation)
        return _answer(3, {
            "screen": screen,
            "safe_navigation_operations": operations,
            "blocking_overlay_actions": [
                step
                for overlay in screen.get("blocking_overlays") or []
                for step in overlay.get("dismiss") or []
            ],
            "allowed_effects": self.catalog.profile.allowed_effects,
        })

    def query_4(self, *, operation: str, **_: Any) -> dict[str, Any]:
        asset = self.catalog.operation(operation)
        claims = [
            row for row in self.catalog.claims()
            if str(row.get("operation") or "") == operation
        ]
        if asset is None and not claims:
            return _no(4, "operation_not_found", operation=operation)
        observations = run_index.knowledge_observations(
            self.run_root, app=self.app, operation=operation
        )
        endpoints = {
            str((row.get("payload") or {}).get("endpoint"))
            for row in observations
            if row.get("type") == "api_call" and (row.get("payload") or {}).get("endpoint")
        }
        for claim in claims:
            endpoints.update(str(value) for value in (claim.get("relations") or {}).get("endpoints") or [])
        witnesses = [
            value
            for claim in claims
            for value in (claim.get("relations") or {}).get("business_states") or []
        ]
        asset = asset or {"name": operation, "status": "knowledge_only"}
        gaps = [] if asset.get("witness") or witnesses else ["postcondition_witness_missing"]
        if not observations:
            gaps.append("native_observation_index_missing")
        return _answer(4, {
            "operation": asset,
            "postcondition_witness": asset.get("witness") or witnesses,
            "screen_expectations": (
                asset.get("screen_expectations") or [
                    value
                    for claim in claims
                    for value in (claim.get("relations") or {}).get("screens") or []
                ]
            ),
            "observed_endpoints": sorted(endpoints),
            "observations": observations,
            "knowledge_claims": claims,
        }, gaps=gaps)

    def query_5(self, *, endpoint: str, **_: Any) -> dict[str, Any]:
        rows = run_index.knowledge_observations(self.run_root, app=self.app, type="api_call")
        matches = [
            row for row in rows
            if endpoint in str((row.get("payload") or {}).get("endpoint") or "")
        ]
        claims = [
            claim for claim in self.catalog.claims()
            if any(
                endpoint in str(value)
                for value in (claim.get("relations") or {}).get("endpoints") or []
            )
        ]
        if not matches and not claims:
            return _no(5, "endpoint_not_observed", endpoint=endpoint)
        return _answer(5, {
            "endpoint": endpoint,
            "action_windows": matches,
            "cases": sorted({str(row.get("case_id")) for row in matches if row.get("case_id")}),
            "operations": sorted(
                {str(row.get("operation")) for row in matches if row.get("operation")}
                | {str(claim.get("operation")) for claim in claims if claim.get("operation")}
            ),
            "knowledge_claims": claims,
        }, gaps=[] if matches else ["native_action_window_missing"])

    def query_6(
        self,
        *,
        run_id: str,
        require_sha_identity: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        candidates = sorted(self.run_root.glob(f"*/{run_id}"))
        if not candidates:
            return _no(6, "target_run_not_found", run_id=run_id)
        target_dir = candidates[-1]
        baseline = run_index.find_same_build_pass(
            self.run_root, target_dir, require_sha_identity=require_sha_identity
        )
        if baseline.get("status") != "found":
            return _no(6, str(baseline.get("status") or "no_answer"), **baseline)
        target = read_json(target_dir / "platform-run-result.json") or {}
        base_dir = Path(str(baseline["baseline_run_dir"]))
        base = read_json(base_dir / "platform-run-result.json") or {}
        return _answer(6, {**baseline, "diff": _result_diff(base, target)})

    def query_7(
        self,
        *,
        current_subject_build_key: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        active = [row for row in self.catalog.claims() if row.get("status") == "active"]
        projections = [
            {"claim_id": row.get("id"), **evidence_state(
                row, current_subject_build_key=current_subject_build_key
            )}
            for row in active
        ]
        return _answer(7, {
            "soft_hold": [row for row in projections if row["evidence_state"] != "current"],
            "unchanged_asset_status": True,
        })

    def query_8(self, *, slot: str, **_: Any) -> dict[str, Any]:
        inv = self.catalog.inventory()
        # A bare slot names that slot's Candidate Build (v1: its only Build).
        target = next(
            (
                row for row in inv["builds"]
                if str(row.get("slot")) == slot and row.get("candidate")
            ),
            None,
        )
        if target is None:
            return _no(8, "registered_build_not_found", slot=slot)
        if target.get("default"):
            return _no(8, "build_already_active", slot=slot)
        claims = [row for row in self.catalog.claims() if row.get("status") == "active"]
        golden_claims = [
            row for row in claims
            if str(row.get("kind") or "").startswith("golden_chain")
        ]
        return _answer(8, {
            "build": target,
            "screens_to_recapture": [row.get("id") for row in self.catalog.screens() if row.get("status") == "active"],
            "operations_to_replay": [row.get("name") for row in self.catalog.operations() if row.get("status") == "active"],
            "claims_to_refresh": [row.get("id") for row in claims],
            "golden_chains": (
                [row.get("id") for row in self.catalog.cases() if row.get("status") == "active"]
                + [row.get("id") for row in golden_claims]
            ),
        })
