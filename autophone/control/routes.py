"""Shared Control Plane HTTP routes without device-side dependencies."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .postgres import ControlStateError
from .interface import ControlPlaneInterface
from .host_coordination import HostCoordination


ControlProvider = Callable[[], ControlPlaneInterface]
ActorProvider = Callable[[], str]


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        value != value.strip()
        or not value
        or len(value) > 200
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_idempotency_key",
            "message": (
                "Idempotency-Key must be 1-200 visible ASCII characters "
                "without surrounding whitespace"
            ),
        })
    return value


def create_control_router(
    control_provider: ControlProvider,
    *,
    actor_provider: ActorProvider = lambda: "local",
    host_provider: Callable[[], HostCoordination] | None = None,
) -> APIRouter:
    """Create the one HTTP Interface shared by standalone and combined apps."""

    router = APIRouter()
    hosts = host_provider or control_provider

    @router.post("/api/control/assignments")
    def submit(request: Request, payload: dict | None = None) -> dict[str, Any]:
        body = payload or {}
        header_key = validate_idempotency_key(request.headers.get("idempotency-key"))
        body_key = validate_idempotency_key(
            str(body.get("idempotency_key"))
            if body.get("idempotency_key") is not None
            else None
        )
        key = header_key or body_key or ""
        plan = body.get("plan") if isinstance(body.get("plan"), dict) else body
        try:
            return control_provider().submit_assignment(plan, key)
        except ControlStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/control/assignments/{assignment_id}")
    def get_assignment(assignment_id: str) -> dict[str, Any]:
        try:
            return control_provider().get_assignment(assignment_id)
        except ControlStateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/control/assignments/{assignment_id}/cancel")
    def cancel_assignment(
        assignment_id: str,
        payload: dict | None = None,
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return control_provider().cancel_assignment(
                assignment_id,
                str(body.get("actor") or actor_provider()),
                str(body.get("reason") or ""),
            )
        except ControlStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/control/assignments/{assignment_id}/retry")
    def retry_assignment(assignment_id: str) -> dict[str, Any]:
        try:
            return control_provider().retry_assignment(assignment_id)
        except ControlStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/control/rigs")
    def list_rigs(include_deregistered: bool = False) -> dict[str, Any]:
        control = control_provider()
        return {"rigs": control.list_rigs(include_deregistered=True)
                if include_deregistered else control.list_rigs()}

    @router.post("/api/control/rigs/{rig_id}/deregister")
    def deregister_rig(rig_id: str, payload: dict | None = None) -> dict[str, Any]:
        body = payload or {}
        try:
            return control_provider().deregister_rig(
                rig_id, int(body.get("rig_generation") or 0),
                actor=str(body.get("actor") or actor_provider()),
                reason=str(body.get("reason") or ""),
            )
        except (ControlStateError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/control/rigs/{rig_id}/recover")
    def recover_rig(rig_id: str, payload: dict | None = None) -> dict[str, Any]:
        body = payload or {}
        try:
            return control_provider().recover_rig(
                rig_id,
                int(body.get("rig_generation") or 0),
                str(body.get("actor") or actor_provider()),
                dict(body.get("probe_result") or {}),
            )
        except (ControlStateError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/control/reservations")
    def create_reservation(payload: dict | None = None) -> dict[str, Any]:
        try:
            return control_provider().create_reservation(payload or {})
        except ControlStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/control/reservations/{reservation_id}/release")
    def release_reservation(
        reservation_id: str,
        payload: dict | None = None,
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return control_provider().release_reservation(
                reservation_id,
                str(body.get("actor") or actor_provider()),
            )
        except ControlStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/control/hosts/{host_id}")
    def get_host(host_id: str) -> dict[str, Any]:
        try:
            return hosts().get_host(host_id)
        except ControlStateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/control/hosts/{host_id}/capacity")
    def change_host_capacity(
        host_id: str,
        payload: dict | None = None,
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return hosts().change_host_capacity(
                host_id,
                int(body.get("capacity")),
                actor=str(body.get("actor") or actor_provider()),
                reason=str(body.get("reason") or ""),
                evidence=dict(body.get("evidence") or {}),
                approval=dict(body.get("approval") or {}),
            )
        except (ControlStateError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/control/hosts/{host_id}/maintenance")
    def begin_host_maintenance(
        host_id: str,
        payload: dict | None = None,
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return hosts().begin_host_maintenance(
                host_id,
                intent=str(body.get("intent") or ""),
                owner=str(body.get("owner") or actor_provider()),
                ttl_seconds=int(body.get("ttl_seconds") or 300),
            )
        except (ControlStateError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/control/host-maintenance/{maintenance_id}/complete")
    def finish_host_maintenance(
        maintenance_id: str,
        payload: dict | None = None,
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return hosts().finish_host_maintenance(
                maintenance_id,
                owner=str(body.get("owner") or actor_provider()),
                result=dict(body.get("result") or {}),
            )
        except ControlStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


__all__ = ["create_control_router", "validate_idempotency_key"]
