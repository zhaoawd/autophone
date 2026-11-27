"""Application operations for unified location sessions."""
from __future__ import annotations

from threading import RLock
from typing import Any

from autophone.location.contracts import LocationSample, LocationSessionSpec
from autophone.location.targets import LocationTargetError
from autophone.location.system import SystemLocationError
from autophone.location.checkpoints import LocationCheckpointError
from autophone.location.coordinator import LocationCoordinatorError


class LocationApplicationError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class UnifiedLocationApplication:
    def __init__(
        self,
        *,
        coordinator: Any,
        checkpoint_store: Any,
        control_lock: RLock,
    ) -> None:
        self.coordinator = coordinator
        self.checkpoint_store = checkpoint_store
        self.control_lock = control_lock

    def system_snapshot(self) -> dict:
        return self.coordinator.system_snapshot().as_dict()

    def routes(self) -> dict:
        from autophone.location.route_replay import list_builtin_routes

        return {
            "routes": [{
                "route_id": route.route_id,
                "destination_name": route.destination_name,
                "total_m": route.total_m,
                "origin": {"lat": route.origin.lat, "lng": route.origin.lng},
                "destination": {
                    "lat": route.destination.lat,
                    "lng": route.destination.lng,
                },
                "store_code": str(route.source.get("store_code") or "") or None,
            } for route in list_builtin_routes()]
        }

    def checkpoints(self) -> dict:
        try:
            return {"checkpoints": list(self.checkpoint_store.summaries())}
        except LocationCheckpointError as error:
            raise LocationApplicationError(503, {
                "code": "location_checkpoint_read_failed",
                "message": str(error),
            }) from error

    def restore(self, session_id: str, payload: dict) -> dict:
        owner = self._owner(payload)
        replace = payload.get("replace", False)
        if not isinstance(replace, bool):
            raise LocationApplicationError(422, {
                "code": "location_contract_invalid",
                "message": "replace must be boolean",
            })
        try:
            spec = self.checkpoint_store.session_spec(session_id)
            if spec.owner != owner:
                raise LocationCoordinatorError(
                    "location_owner_conflict",
                    "location checkpoint is owned by another owner",
                )
            with self.control_lock:
                return self.coordinator.start(spec, replace=replace).as_dict()
        except KeyError as error:
            raise LocationApplicationError(404, {
                "code": "location_checkpoint_not_found",
                "message": str(session_id),
            }) from error
        except LocationCheckpointError as error:
            raise LocationApplicationError(503, {
                "code": "location_checkpoint_read_failed",
                "message": str(error),
            }) from error
        except (LocationCoordinatorError, LocationTargetError, SystemLocationError) as error:
            raise self._domain_error(error) from error

    def create(self, payload: dict) -> dict:
        try:
            raw_spec = payload.get("spec", payload)
            if not isinstance(raw_spec, dict):
                raise ValueError("spec must be an object")
            spec = LocationSessionSpec.from_mapping(raw_spec)
            raw_initial = payload.get("initial_sample")
            if raw_initial is not None and not isinstance(raw_initial, dict):
                raise ValueError("initial_sample must be an object")
            initial_sample = (
                LocationSample.from_mapping(raw_initial)
                if raw_initial is not None
                else None
            )
            replace = payload.get("replace", False)
            if not isinstance(replace, bool):
                raise ValueError("replace must be boolean")
            with self.control_lock:
                return self.coordinator.start(
                    spec,
                    initial_sample=initial_sample,
                    replace=replace,
                ).as_dict()
        except ValueError as error:
            raise LocationApplicationError(422, {
                "code": "location_contract_invalid",
                "message": str(error),
            }) from error
        except (LocationCoordinatorError, LocationTargetError, SystemLocationError) as error:
            raise self._domain_error(error) from error

    def get(self, session_id: str) -> dict:
        try:
            return self.coordinator.snapshot(session_id).as_dict()
        except (LocationCoordinatorError, LocationTargetError, SystemLocationError) as error:
            raise self._domain_error(error) from error

    def publish(self, session_id: str, payload: dict) -> dict:
        owner = self._owner(payload)
        try:
            raw_sample = payload.get("sample")
            if not isinstance(raw_sample, dict):
                raise ValueError("sample must be an object")
            sample = LocationSample.from_mapping(raw_sample)
            return self.coordinator.publish(
                session_id,
                sample,
                owner=owner,
            ).as_dict()
        except ValueError as error:
            raise LocationApplicationError(422, {
                "code": "location_contract_invalid",
                "message": str(error),
            }) from error
        except (LocationCoordinatorError, LocationTargetError, SystemLocationError) as error:
            raise self._domain_error(error) from error

    def control(self, session_id: str, payload: dict) -> dict:
        owner = self._owner(payload)
        command = str(payload.get("command") or "").strip()
        if not command:
            raise LocationApplicationError(422, {
                "code": "location_command_required",
                "message": "command is required",
            })
        try:
            return self.coordinator.control(
                session_id,
                command,
                owner=owner,
                value=payload.get("value"),
            ).as_dict()
        except (LocationCoordinatorError, LocationTargetError, SystemLocationError) as error:
            raise self._domain_error(error) from error

    def stop(self, session_id: str, payload: dict) -> dict:
        owner = self._owner(payload)
        try:
            return self.coordinator.stop(session_id, owner=owner).as_dict()
        except (LocationCoordinatorError, LocationTargetError, SystemLocationError) as error:
            raise self._domain_error(error) from error

    @staticmethod
    def _owner(payload: dict) -> str:
        owner = str(payload.get("owner") or "").strip()
        if not owner:
            raise LocationApplicationError(422, {
                "code": "location_owner_required",
                "message": "owner is required",
            })
        return owner

    @staticmethod
    def _domain_error(error: Exception) -> LocationApplicationError:
        code = str(getattr(error, "code", "location_internal_error"))
        if code == "location_session_not_found":
            status = 404
        elif code in {
            "location_access_status_unavailable",
            "location_guest_unavailable",
            "location_protocol_error",
            "location_target_stop_failed",
            "location_checkpoint_read_failed",
            "location_checkpoint_write_failed",
        }:
            status = 503
        elif code in {
            "location_initial_sample_required",
            "location_coordinate_system_mismatch",
            "location_source_unsupported",
            "location_target_kind_mismatch",
            "location_target_not_declared",
            "location_target_unsupported",
            "location_command_unsupported",
            "location_control_value_required",
            "location_control_value_invalid",
            "location_source_invalid",
            "location_source_kind_mismatch",
        }:
            status = 422
        else:
            status = 409
        return LocationApplicationError(
            status,
            {"code": code, "message": str(error)},
        )
