from __future__ import annotations

from autophone.scenario.presets import (
    ScenarioPresetStore,
)
from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:

    def _raise_scenario_http(exc: Exception) -> None:
        if isinstance(exc, (
            deps.ScenarioPlanNotFound, deps.ScenarioSessionNotFound,
            deps.ScenarioPresetNotFound,
        )):
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, (deps.ScenarioDeviceBusy, deps.ScenarioPresetConflict)):
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, (
            deps.ScenarioRuntimeError, deps.ScenarioPresetError, ValueError,
        )):
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        raise exc

    deps._raise_scenario_http = _raise_scenario_http

    def _scenario_preset_store(app_name: str) -> ScenarioPresetStore:
        """通用接口场景 preset 的唯一 HTTP adapter；路由不直接碰 YAML。"""
        return deps.ScenarioPresetStore(deps.load_app_profile(app_name))

    deps._scenario_preset_store = _scenario_preset_store

def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/apps/{app_name}/scenarios/preflight")
    def scenario_preflight(app_name: str, payload: dict) -> dict:
        """通用场景入口：优先锁定 preset version；inline 仅保留诊断/迁移用途。"""
        try:
            preset_binding = None
            preset_id = str(payload.get("preset_id") or "").strip()
            if preset_id:
                if "scenario" in payload:
                    raise deps.ScenarioPresetError("preset preflight 不得同时传 scenario")
                scenario, preset_binding = deps._scenario_preset_store(app_name).build_scenario(
                    preset_id,
                    version=payload.get("version"),
                    scenario_id=payload.get("scenario_id"),
                )
            else:
                scenario = payload.get("scenario")
                if not isinstance(scenario, dict):
                    raise deps.ScenarioPresetError("scenario 或 preset_id 必须提供一个")
            selected_mode = str(payload.get("mode") or "attach")
            result = deps.scenario_runtime.preflight(
                app_name,
                scenario,
                mode=selected_mode,
                surface_policy=str(payload.get("surface_policy") or "recover_home"),
                device=str(payload.get("device") or deps.DEFAULT_DEVICE),
                evidence={
                    "kind": "api_scenario",
                    "preset": preset_binding,
                    # 这是待由独立上游 observer 证明的声明，不是零触达证据。
                    "upstream_policy": "local_short_circuit_requires_observation",
                    "recovery_policy": (
                        "attach_requires_post_detach_observation"
                        if selected_mode.strip().lower() == "attach" else "not_applicable"
                    ),
                },
            )
            if preset_binding is not None:
                result["preset"] = preset_binding
            # UI confirmation only gets the routing projection.  Response bodies,
            # headers and query values stay out of the LaunchPlan surface.
            result["targets"] = [
                {
                    "rule_id": str(rule.get("rule_id") or ""),
                    "method": str((rule.get("match") or {}).get("method") or ""),
                    "host": str((rule.get("match") or {}).get("host") or ""),
                    "path": str(
                        (rule.get("match") or {}).get("path")
                        or (rule.get("match") or {}).get("path_prefix")
                        or ""
                    ),
                }
                for rule in scenario.get("rules", [])
            ]
            return result
        except Exception as err:  # noqa: BLE001 - 映射 module error 到稳定 HTTP 语义
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_preflight = scenario_preflight

    @router.get("/api/apps/{app_name}/scenarios")
    def scenario_presets(
        app_name: str, env: str | None = None, include_deprecated: bool = False,
    ) -> dict:
        try:
            return {"presets": deps._scenario_preset_store(app_name).list(
                env=env, include_deprecated=include_deprecated,
            )}
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_presets = scenario_presets

    @router.post("/api/apps/{app_name}/scenarios")
    def scenario_preset_create(app_name: str, payload: dict) -> dict:
        try:
            profile = deps.load_app_profile(app_name)
            return deps._scenario_preset_store(app_name).create(
                name=payload.get("name"),
                selected_env=payload.get("selected_env") or profile.env,
                rules=payload.get("rules"),
                source=payload.get("source") or "manual",
                source_ref=payload.get("source_ref"),
                note=payload.get("note"),
                status=payload.get("status") or "draft",
            )
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_preset_create = scenario_preset_create

    @router.post("/api/apps/{app_name}/scenarios/validate")
    def scenario_preset_validate(app_name: str, payload: dict) -> dict:
        try:
            profile = deps.load_app_profile(app_name)
            return deps._scenario_preset_store(app_name).validate_rules(
                selected_env=payload.get("selected_env") or profile.env,
                rules=payload.get("rules"),
            )
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    @router.get("/api/apps/{app_name}/scenarios/{preset_id}")
    def scenario_preset_get(app_name: str, preset_id: str, version: int | None = None) -> dict:
        try:
            return deps._scenario_preset_store(app_name).get(preset_id, version=version)
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_preset_get = scenario_preset_get

    @router.patch("/api/apps/{app_name}/scenarios/{preset_id}")
    def scenario_preset_update(app_name: str, preset_id: str, payload: dict) -> dict:
        try:
            return deps._scenario_preset_store(app_name).update(preset_id, payload)
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_preset_update = scenario_preset_update

    @router.delete("/api/apps/{app_name}/scenarios/{preset_id}")
    def scenario_preset_delete(app_name: str, preset_id: str, payload: dict | None = None) -> dict:
        try:
            return deps._scenario_preset_store(app_name).deprecate(
                preset_id, reason=(payload or {}).get("reason"),
                expected_revision=(payload or {}).get("expected_revision"),
            )
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_preset_delete = scenario_preset_delete

    @router.post("/api/scenario-plans/{plan_id}/start")
    def scenario_start(plan_id: str, payload: dict | None = None) -> dict:
        try:
            return deps.scenario_runtime.start(
                plan_id,
                ttl_s=float((payload or {}).get("ttl_s") or 120.0),
            )
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_start = scenario_start

    @router.get("/api/scenario-sessions/{session_id}")
    def scenario_status(session_id: str) -> dict:
        try:
            return deps.scenario_runtime.status(session_id)
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_status = scenario_status

    @router.post("/api/scenario-sessions/{session_id}/close")
    def scenario_close(session_id: str) -> dict:
        try:
            return deps.scenario_runtime.close(session_id)
        except Exception as err:  # noqa: BLE001
            deps._raise_scenario_http(err)
            raise AssertionError("unreachable")

    deps.scenario_close = scenario_close

    return router
