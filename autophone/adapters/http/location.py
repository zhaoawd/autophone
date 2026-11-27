from __future__ import annotations

from autophone.application.location import (
    UnifiedLocationApplication,
)
from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:

    def _unified_location() -> UnifiedLocationApplication:
        return deps.UnifiedLocationApplication(
            coordinator=deps.location_session_coordinator,
            checkpoint_store=deps.location_checkpoint_store,
            control_lock=deps.location_control_mu,
        )

    deps._unified_location = _unified_location

    def _run_location(operation):
        try:
            return operation()
        except deps.LocationApplicationError as error:
            raise deps.HTTPException(
                status_code=error.status_code,
                detail=error.detail,
            ) from error

    deps._run_location = _run_location


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/location/sessions")
    def list_location_sessions() -> dict:
        """返回统一定位能力与会话只读模型。"""
        return deps._unified_location().system_snapshot()

    deps.list_location_sessions = list_location_sessions

    @router.get("/api/location/devices/{device}/current")
    def current_location(device: str) -> dict:
        response = deps._device_session_call(device, "system_location", {"t": "location_current"})
        if response.get("ok") is not True:
            raise deps.HTTPException(status_code=503, detail=response)
        return {"device": device, "reading": response}

    @router.get("/api/location/routes")
    def list_location_routes() -> dict:
        """返回可由统一 route source 使用的 tracked fixture 摘要。"""
        return deps._unified_location().routes()

    deps.list_location_routes = list_location_routes

    @router.get("/api/location/checkpoints")
    def list_location_checkpoints() -> dict:
        """返回可显式恢复的 ACK-committed route checkpoint 摘要。"""
        return deps._run_location(deps._unified_location().checkpoints)

    deps.list_location_checkpoints = list_location_checkpoints

    @router.post("/api/location/checkpoints/{session_id}/restore")
    def restore_location_checkpoint(
        session_id: str,
        payload: dict | None = None,
    ) -> dict:
        """按原规格恢复 route；恢复后保持 paused，必须由同一 owner 显式 resume。"""
        application = deps._unified_location()
        return deps._run_location(lambda: application.restore(session_id, payload or {}))

    deps.restore_location_checkpoint = restore_location_checkpoint

    @router.post("/api/location/sessions")
    def create_location_session(payload: dict | None = None) -> dict:
        """创建定点或流式定位会话，并声明全部投递目标。"""
        application = deps._unified_location()
        return deps._run_location(lambda: application.create(payload or {}))

    deps.create_location_session = create_location_session

    @router.get("/api/location/sessions/{session_id}")
    def get_location_session(session_id: str) -> dict:
        application = deps._unified_location()
        return deps._run_location(lambda: application.get(session_id))

    deps.get_location_session = get_location_session

    @router.post("/api/location/sessions/{session_id}/samples")
    def publish_location_sample(session_id: str, payload: dict | None = None) -> dict:
        """投递下一条权威样本；必需目标全部 ACK 后才推进会话序号。"""
        application = deps._unified_location()
        return deps._run_location(lambda: application.publish(session_id, payload or {}))

    deps.publish_location_sample = publish_location_sample

    @router.patch("/api/location/sessions/{session_id}")
    def control_location_session(session_id: str, payload: dict | None = None) -> dict:
        """控制定位会话或受管来源；owner、命令和值不匹配时 fail-closed。"""
        application = deps._unified_location()
        return deps._run_location(lambda: application.control(session_id, payload or {}))

    deps.control_location_session = control_location_session

    @router.delete("/api/location/sessions/{session_id}")
    def stop_location_session(session_id: str, payload: dict | None = None) -> dict:
        application = deps._unified_location()
        return deps._run_location(lambda: application.stop(session_id, payload or {}))

    deps.stop_location_session = stop_location_session

    @router.get("/api/vm/location/policies")
    def list_app_location_policies() -> dict:
        """App 级自动定位策略：公开版不内置 App 运行时定位注入 provider，恒返回不可用。"""
        return {
            "schema": "autophone.app-location-policies/v1",
            "available": False,
            "running": False,
            "policies": [],
        }

    deps.list_app_location_policies = list_app_location_policies

    @router.get("/api/vm/location/points")
    def list_location_points(kind: str | None = None) -> dict:
        """模拟点 / 送餐常用点列表。门店查询由可选交付扩展提供。"""
        try:
            points = deps.location_points.load_points()
        except deps.location_points.PointRegistryError as exc:
            raise deps.HTTPException(status_code=500, detail=f"location points registry error: {exc}") from exc
        if kind:
            points = [p for p in points if p.get("kind") == kind]
        return {"points": points}

    deps.list_location_points = list_location_points

    @router.post("/api/vm/location/points")
    def add_location_point(payload: dict | None = None) -> dict:
        """收藏一个常用点（只写本机 overlay）。id 已存在 → 409。"""
        payload = payload or {}
        try:
            point = deps.location_points.add_point(payload)
        except deps.location_points.PointExistsError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        except deps.location_points.PointRegistryError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return {"point": point}

    deps.add_location_point = add_location_point

    @router.delete("/api/vm/location/points/{point_id}")
    def delete_location_point(point_id: str) -> dict:
        """删除一个常用点（只删 overlay）。seed 只读 → 409；不存在 → 404。"""
        try:
            deps.location_points.remove_point(point_id)
        except deps.location_points.PointReadonlyError as exc:
            raise deps.HTTPException(status_code=409, detail="readonly_seed_point") from exc
        except deps.location_points.PointNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=f"unknown point: {point_id}") from exc
        return {"ok": True, "removed": point_id}

    deps.delete_location_point = delete_location_point

    return router
