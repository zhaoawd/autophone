from __future__ import annotations

from fastapi import APIRouter, Body, Request
from typing import Any

from autophone.logging import get_logger

from .deps import HttpDeps

log = get_logger(__name__)

def install_helpers(deps: HttpDeps) -> None:
    pass


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/apps/{app_name}/knowledge/inventory")
    def knowledge_inventory(app_name: str) -> dict:
        from autophone.knowledge.inventory import inventory

        try:
            return inventory(app_name)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.knowledge_inventory = knowledge_inventory

    @router.get("/api/apps/{app_name}/knowledge/claims")
    def knowledge_claims(app_name: str) -> dict:
        from autophone.knowledge.store import load_claims

        try:
            deps.load_app_profile(app_name)
            return load_claims(app_name)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.knowledge_claims = knowledge_claims

    @router.get("/api/apps/{app_name}/knowledge/exception-candidates")
    def exception_candidates(app_name: str, request: Request, key: str | None = None) -> dict:
        """Fresh draft preview; no catalog writes, devices, or experimental kinds."""
        if set(request.query_params) - {"key"} or len(request.query_params.getlist("key")) > 1:
            raise deps.HTTPException(status_code=422, detail="only one optional key is supported")
        if key is not None and (not key.strip() or len(key) > 512):
            raise deps.HTTPException(status_code=422, detail="key must contain 1..512 characters")
        return _exception_candidates(app_name, key)

    def _exception_candidates(app_name: str, key: str | None) -> dict:
        import yaml

        from autophone.knowledge.exception_candidates import propose_exception_candidates

        try:
            profile = deps.load_app_profile(app_name)
            snapshot = deps._exception_view(profile).current(include_muted=True, experimental=False)
            return propose_exception_candidates(snapshot, key=key)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
            log.warning("exception candidates unavailable app=%s: %s", app_name, exc, exc_info=True)
            raise deps.HTTPException(status_code=422, detail={
                "code": "exception_candidates_unavailable", "message": str(exc),
            }) from exc

    def _single_optional_query(request: Request, name: str, value: str | None) -> None:
        if set(request.query_params) - {name} or len(request.query_params.getlist(name)) > 1:
            raise deps.HTTPException(status_code=422, detail=f"only one optional {name} is supported")
        if value is not None and (not value.strip() or len(value) > 512):
            raise deps.HTTPException(status_code=422, detail=f"{name} must contain 1..512 characters")

    @router.get("/api/apps/{app_name}/knowledge/business-validity-candidates")
    def business_validity_candidates(
        app_name: str, request: Request, case_id: str | None = None,
    ) -> dict:
        """BL-038 D2 draft preview; reads Cases, Fixture declarations and Run verdicts only."""
        _single_optional_query(request, "case_id", case_id)
        return _business_validity_candidates(app_name, case_id)

    def _business_validity_candidates(app_name: str, case_id: str | None) -> dict:
        import yaml

        from autophone.assets.fixture_requirements import load_fixture_requirements
        from autophone.knowledge.business_validity_candidates import (
            load_case_rows,
            propose_business_validity_candidates,
        )

        try:
            profile = deps.load_app_profile(app_name)
            cases = load_case_rows(profile.cases_path())
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
            log.warning("business validity candidates unavailable app=%s: %s", app_name, exc, exc_info=True)
            raise deps.HTTPException(status_code=422, detail={
                "code": "business_validity_candidates_unavailable", "message": str(exc),
            }) from exc
        try:
            verdicts = deps.service.run_catalog.case_verdicts(app_name)
        except Exception as exc:  # noqa: BLE001 - fail closed: empty preview with a reason
            log.warning("run verdicts unavailable app=%s: %s", app_name, exc, exc_info=True)
            verdicts = None
        return propose_business_validity_candidates(
            app_name, cases,
            case_verdicts=verdicts,
            fixture_requirements=load_fixture_requirements(profile),
            build_label=getattr(profile, "build_label", None),
            case_id=case_id,
        )

    @router.get("/api/apps/{app_name}/knowledge/duplicate-path-hints")
    def duplicate_path_hints(
        app_name: str, request: Request, recipe_id: str | None = None,
    ) -> dict:
        """BL-038 D2 draft preview; compares Recipe steps, never writes an Operation."""
        from autophone.knowledge.duplicate_path_hints import (
            load_operation_rows,
            load_recipe_sources,
            propose_duplicate_path_hints,
        )

        _single_optional_query(request, "recipe_id", recipe_id)
        try:
            profile = deps.load_app_profile(app_name)
            recipes, skipped = load_recipe_sources(profile.recipes_dir())
            operations = load_operation_rows(profile.operations_path())
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("duplicate path hints unavailable app=%s: %s", app_name, exc, exc_info=True)
            raise deps.HTTPException(status_code=422, detail={
                "code": "duplicate_path_hints_unavailable", "message": str(exc),
            }) from exc
        return propose_duplicate_path_hints(
            app_name, recipes, operations, recipe_id=recipe_id, skipped=skipped,
        )

    @router.post("/api/apps/{app_name}/knowledge/candidate-adoptions")
    def adopt_knowledge_candidate(app_name: str, payload: Any = Body(None)) -> dict:
        """BL-038 D3: write one current D1/D2 candidate as a draft Claim with verified evidence."""
        from autophone.knowledge import candidate_adoption as adoption
        from autophone.knowledge.store import ClaimExistsError, append_draft

        try:
            profile = deps.load_app_profile(app_name)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            candidate_id, echoed, refs = adoption.parse_request(payload)
            source = adoption.candidate_source(candidate_id)
            case_id = str((echoed.get("scope") or {}).get("case_id") or "").strip()
            if source == "exception_candidates":
                key = str(((echoed.get("provenance") or {}).get("exception") or {}).get("key") or "").strip()
                document = _exception_candidates(app_name, key or None) if key else {"claims": []}
            else:
                document = _business_validity_candidates(app_name, case_id or None) if case_id else {"claims": []}
            candidate = adoption.current_candidate(document, candidate_id, echoed)
            resolved = adoption.resolve_observations(
                deps.service.run_root, app_name, str(candidate["scope"]["case_id"]), refs,
            )
            claim, decision = adoption.build_adopted_claim(
                candidate, resolved, actor=deps._current_actor() if deps._current_actor else None,
            )
            try:
                path = append_draft(app_name, claim, root=profile.root)
            except ClaimExistsError as exc:
                raise adoption.AdoptionError(
                    409, "claim_already_exists", "the candidate was already adopted", claim_id=str(exc),
                ) from exc
        except adoption.AdoptionError as exc:
            log.warning("knowledge candidate adoption rejected app=%s code=%s: %s",
                        app_name, exc.detail.get("code"), exc)
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        log.info("knowledge candidate adopted app=%s claim=%s level=%s refs=%d",
                 app_name, claim["id"], claim["evidence_level"], len(claim["evidence_refs"]))
        return {
            "app": app_name,
            "claim": claim,
            "verifier": decision,
            "catalog": str(path),
            "rules": adoption.rules(),
        }

    deps.adopt_knowledge_candidate = adopt_knowledge_candidate

    @router.post("/api/apps/{app_name}/knowledge/compile")
    def knowledge_compile(app_name: str, payload: dict | None = None) -> dict:
        from autophone.application import run_index
        from autophone.knowledge.store import merge_drafts
        from autophone.observation.compiler import compile_and_write, propose

        body = payload or {}
        run_id = str(body.get("run_id") or "").strip()
        if not run_id:
            raise deps.HTTPException(status_code=422, detail="run_id is required")
        try:
            run_dir = deps.service.queries.find_run_dir(run_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        spec = deps.read_json(run_dir / "run-spec.json") or {}
        if spec.get("app") and str(spec.get("app")) != app_name:
            raise deps.HTTPException(status_code=409, detail="run app does not match route app")
        path = compile_and_write(run_dir, app=app_name)
        indexed = (run_dir / "platform-run-result.json").is_file()
        if indexed:
            run_index.ingest_run(run_dir, run_root=deps.service.run_root)
        observations = deps.read_json(path) or {}
        drafts = propose(observations) if body.get("propose") or body.get("write_catalog") else []
        result: dict[str, Any] = {
            "observations": str(path),
            "count": len(observations.get("observations") or []),
            "indexed": indexed,
            "draft_claims": drafts,
        }
        if body.get("write_catalog"):
            result["catalog"] = str(merge_drafts(app_name, drafts))
        return result

    deps.knowledge_compile = knowledge_compile

    @router.post("/api/apps/{app_name}/knowledge/verify")
    def knowledge_verify(app_name: str, payload: dict | None = None) -> dict:
        from autophone.knowledge.evidence_verifier import verify
        from autophone.knowledge.store import load_claims

        body = payload or {}
        observations: list[dict[str, Any]] = []
        for path in deps.service.run_root.glob("*/*/observations.json"):
            try:
                observations.extend((deps.read_json(path) or {}).get("observations") or [])
            except (OSError, deps.json.JSONDecodeError):
                continue
        claims = load_claims(app_name).get("claims") or []
        return {
            "app": app_name,
            "decisions": [
                verify(
                    claim,
                    observations,
                    current_subject_build_key=body.get("subject_build_key"),
                )
                for claim in claims
            ],
        }

    deps.knowledge_verify = knowledge_verify

    @router.post("/api/apps/{app_name}/knowledge/query")
    def knowledge_query(app_name: str, payload: dict | None = None) -> dict:
        from autophone.knowledge.query import KnowledgeQueryEngine

        body = payload or {}
        try:
            number = int(body.get("number"))
        except (TypeError, ValueError) as exc:
            raise deps.HTTPException(status_code=422, detail="number must be 1..8") from exc
        if number not in range(1, 9):
            raise deps.HTTPException(status_code=422, detail="number must be 1..8")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise deps.HTTPException(status_code=422, detail="params must be an object")
        try:
            return KnowledgeQueryEngine(
                app_name, run_root=deps.service.run_root
            ).execute(number, **params)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.knowledge_query = knowledge_query

    @router.get("/api/apps/{app_name}/knowledge/preparation-card/{case_id}")
    def knowledge_preparation_card(
        app_name: str, case_id: str, trial_draft_recipes: bool = False,
    ) -> dict:
        """Unified preparation card; read only, never runs a Trial."""
        import yaml

        from autophone.knowledge.preparation_card import build_preparation_card

        try:
            card = build_preparation_card(
                app_name, case_id,
                trial_draft_recipes=trial_draft_recipes,
                run_root=deps.service.run_root,
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            # Malformed Case / Recipe / Fixture assets: report a structured failure
            # instead of a bare 500, so the dashboard never renders it as ready.
            # TypeError may also be a code defect, so keep the traceback in the log.
            log.warning(
                "preparation card unavailable app=%s case_id=%s: %s",
                app_name, case_id, exc, exc_info=True,
            )
            raise deps.HTTPException(status_code=422, detail={
                "code": "preparation_card_unavailable",
                "message": f"{type(exc).__name__}: {exc}",
            }) from exc
        if card.get("status") == "case_not_found":
            raise deps.HTTPException(status_code=404, detail=f"case not found: {case_id}")
        return card

    deps.knowledge_preparation_card = knowledge_preparation_card

    @router.get("/api/apps/{app_name}/knowledge/view")
    def knowledge_view(
        app_name: str, status: str | None = None, trial_draft_recipes: bool = False,
    ) -> dict:
        """App knowledge view; read only, never runs a Trial.

        ``status`` is an optional comma-separated Case status filter.  A malformed
        Case surfaces as a row error inside the view; only App-level failures
        (unknown App, malformed Case registry) end the request.  When every
        selected row is unavailable the view still returns 200 and carries a
        top-level ``error`` ``{code, message}``.
        """
        import yaml

        from autophone.knowledge.app_view import build_app_knowledge_view

        statuses = [item.strip() for item in (status or "").split(",") if item.strip()]
        try:
            return build_app_knowledge_view(
                app_name,
                statuses=statuses or None,
                trial_draft_recipes=trial_draft_recipes,
                run_root=deps.service.run_root,
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            log.warning("knowledge view unavailable app=%s: %s", app_name, exc, exc_info=True)
            raise deps.HTTPException(status_code=422, detail={
                "code": "knowledge_view_unavailable",
                "message": f"{type(exc).__name__}: {exc}",
            }) from exc

    deps.knowledge_view = knowledge_view

    @router.get("/api/apps/{app_name}/knowledge/fixture-requirements")
    def knowledge_fixture_requirements(app_name: str) -> dict:
        from autophone.assets.fixture_requirements import validate_app_fixture_requirements

        try:
            return validate_app_fixture_requirements(deps.load_app_profile(app_name))
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.knowledge_fixture_requirements = knowledge_fixture_requirements

    @router.get("/api/apps/{app_name}/knowledge/golden-paths/{path_id}/plan")
    def knowledge_golden_path_plan(app_name: str, path_id: str) -> dict:
        from autophone.knowledge.golden_path import GoldenPathError, plan_golden_path

        try:
            return plan_golden_path(app_name, path_id, run_root=deps.service.run_root)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except GoldenPathError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.knowledge_golden_path_plan = knowledge_golden_path_plan

    @router.post("/api/apps/{app_name}/knowledge/golden-paths/{path_id}/verify")
    def knowledge_golden_path_verify(
        app_name: str, path_id: str, payload: dict | None = None,
    ) -> dict:
        """Check a settled Run against the golden path evidence contract (read only)."""
        from autophone.knowledge.golden_path import GoldenPathError, verify_golden_path_run

        run_id = str((payload or {}).get("run_id") or "").strip()
        if not run_id:
            raise deps.HTTPException(status_code=422, detail="run_id is required")
        try:
            run_dir = deps.service.queries.find_run_dir(run_id)
            return verify_golden_path_run(app_name, path_id, run_dir)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except GoldenPathError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.knowledge_golden_path_verify = knowledge_golden_path_verify

    @router.post("/api/apps/{app_name}/knowledge/registry-diff")
    def knowledge_registry_diff(app_name: str, payload: dict | None = None) -> dict:
        from autophone.packages import store as packages
        from autophone.knowledge.diff import registry_diff

        body = payload or {}
        manifest = packages.load_manifest(app_name)
        return registry_diff(
            manifest,
            manifest,
            before_slot=str(body.get("before_slot") or "") or None,
            after_slot=str(body.get("after_slot") or "") or None,
        )

    deps.knowledge_registry_diff = knowledge_registry_diff

    @router.post("/api/apps/{app_name}/knowledge/static-diff")
    def knowledge_static_diff(app_name: str, payload: dict | None = None) -> dict:
        from autophone.knowledge.diff import static_diff

        body = payload or {}
        before, after = body.get("before"), body.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise deps.HTTPException(status_code=422, detail="before and after snapshots are required")
        return static_diff(before, after)

    deps.knowledge_static_diff = knowledge_static_diff

    @router.post("/api/apps/{app_name}/knowledge/coverage")
    def knowledge_coverage(app_name: str, payload: dict | None = None) -> dict:
        from autophone.knowledge.coverage import evaluate, load_coverage

        deps.load_app_profile(app_name)
        body = payload or {}
        cells = body.get("cells")
        if cells is None:
            cells = load_coverage(app_name).get("cells") or []
        if not isinstance(cells, list):
            raise deps.HTTPException(status_code=422, detail="cells must be an array")
        return evaluate(cells)

    deps.knowledge_coverage = knowledge_coverage

    return router
