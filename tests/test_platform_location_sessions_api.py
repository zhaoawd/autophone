from __future__ import annotations

import pytest
from fastapi import HTTPException

from autophone.adapters.http.app import dependencies as api
from autophone.location.checkpoints import LocationCheckpointStore
from autophone.location.contracts import (
    LocationSessionSpec,
    LocationTargetSpec,
)
from autophone.location.coordinator import LocationSessionCoordinator
from autophone.location.targets import FakeLocationTargetAdapter
from autophone.location.sources import RouteLocationSourceDriver


ROUTE_ID = "example-demo-route-1p8km-driving"


def _coordinator() -> LocationSessionCoordinator:
    def factory(
        _session: LocationSessionSpec,
        _target: LocationTargetSpec,
        _replace: bool,
    ) -> FakeLocationTargetAdapter:
        return FakeLocationTargetAdapter(clock=lambda: 10.0)

    return LocationSessionCoordinator(factory)


def _route_coordinator() -> LocationSessionCoordinator:
    def factory(
        _session: LocationSessionSpec,
        _target: LocationTargetSpec,
        _replace: bool,
    ) -> FakeLocationTargetAdapter:
        return FakeLocationTargetAdapter(clock=lambda: 10.0)

    return LocationSessionCoordinator(
        factory,
        source_driver_factory=(
            lambda session, emit: RouteLocationSourceDriver(
                session, emit, run_async=False
            )
        ),
    )


def _spec(session_id: str = "session-1", owner: str = "run:R1") -> dict:
    return {
        "session_id": session_id,
        "owner": owner,
        "device": "vphone-0",
        "source": {
            "source_id": f"source:{session_id}",
            "kind": "external_stream",
            "coordinate_system": "gcj02",
            "watchdog_s": 3,
        },
        "targets": [{
            "target_id": "system:vphone-0",
            "kind": "system_core_location",
            "coordinate_system": "wgs84",
        }],
    }


def _sample(sequence: int = 0) -> dict:
    return {
        "producer_sequence": sequence,
        "emitted_wall_at": f"2026-08-09T10:00:{sequence:02d}+08:00",
        "emitted_monotonic_s": float(sequence),
        "coordinate": {
            "latitude": 31.2304,
            "longitude": 121.4737,
            "coordinate_system": "gcj02",
        },
        "speed_mps": 2,
        "course_deg": 90,
    }


def _route_spec() -> dict:
    return {
        "session_id": "route-session-1",
        "owner": "run:R1",
        "device": "vphone-0",
        "source": {
            "source_id": f"route:{ROUTE_ID}",
            "kind": "route",
            "coordinate_system": "gcj02",
            "route_id": ROUTE_ID,
            "time_scale": 1,
            "watchdog_s": 3,
        },
        "targets": [{
            "target_id": "system:vphone-0",
            "kind": "system_core_location",
            "coordinate_system": "wgs84",
        }],
    }


def test_unified_session_api_create_publish_read_list_and_stop(monkeypatch) -> None:
    coordinator = _coordinator()
    monkeypatch.setattr(api, "location_session_coordinator", coordinator)

    created = api.create_location_session({"spec": _spec()})
    assert created["state"] == "waiting_target"

    delivery = api.publish_location_sample(
        "session-1", {"owner": "run:R1", "sample": _sample()}
    )
    assert delivery["required_accepted"] is True
    assert delivery["receipts"][0]["producer_sequence"] == 0
    assert api.get_location_session("session-1")["state"] == "running"

    paused = api.control_location_session(
        "session-1", {"owner": "run:R1", "command": "pause"}
    )
    assert paused["state"] == "paused"
    resumed = api.control_location_session(
        "session-1", {"owner": "run:R1", "command": "resume"}
    )
    assert resumed["state"] == "running"

    listed = api.list_location_sessions()
    assert listed["schema_version"] == "autophone.location-system/v1"
    assert listed["sessions"][0]["spec"]["session_id"] == "session-1"

    stopped = api.stop_location_session("session-1", {"owner": "run:R1"})
    assert stopped["state"] == "stopped"
    assert api.list_location_sessions()["sessions"] == []

    recreated = api.create_location_session({"spec": _spec()})
    assert recreated["state"] == "waiting_target"


def test_unified_session_api_maps_claim_and_owner_conflicts(monkeypatch) -> None:
    coordinator = _coordinator()
    monkeypatch.setattr(api, "location_session_coordinator", coordinator)
    api.create_location_session({"spec": _spec()})

    with pytest.raises(HTTPException) as claim:
        api.create_location_session({"spec": _spec("session-2", "run:R2")})
    assert claim.value.status_code == 409
    assert claim.value.detail["code"] == "location_target_claim_conflict"

    with pytest.raises(HTTPException) as owner:
        api.publish_location_sample(
            "session-1", {"owner": "run:other", "sample": _sample()}
        )
    assert owner.value.status_code == 409
    assert owner.value.detail["code"] == "location_owner_conflict"


@pytest.mark.parametrize(
    "payload",
    [
        {"spec": "invalid"},
        {"spec": _spec(), "replace": "yes"},
        {"spec": {**_spec(), "targets": "invalid"}},
    ],
)
def test_unified_session_api_rejects_invalid_contract(
    monkeypatch, payload: dict
) -> None:
    monkeypatch.setattr(api, "location_session_coordinator", _coordinator())
    with pytest.raises(HTTPException) as caught:
        api.create_location_session(payload)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "location_contract_invalid"


def test_unified_session_api_requires_owner_and_maps_missing_session(monkeypatch) -> None:
    monkeypatch.setattr(api, "location_session_coordinator", _coordinator())
    with pytest.raises(HTTPException) as owner:
        api.publish_location_sample("missing", {"sample": _sample()})
    assert owner.value.status_code == 422
    assert owner.value.detail["code"] == "location_owner_required"

    with pytest.raises(HTTPException) as missing:
        api.get_location_session("missing")
    assert missing.value.status_code == 404
    assert missing.value.detail["code"] == "location_session_not_found"


def test_unified_session_api_validates_control_command_and_state(monkeypatch) -> None:
    coordinator = _coordinator()
    monkeypatch.setattr(api, "location_session_coordinator", coordinator)
    api.create_location_session({"spec": _spec()})

    with pytest.raises(HTTPException) as missing:
        api.control_location_session("session-1", {"owner": "run:R1"})
    assert missing.value.status_code == 422
    assert missing.value.detail["code"] == "location_command_required"

    with pytest.raises(HTTPException) as unsupported:
        api.control_location_session(
            "session-1", {"owner": "run:R1", "command": "hold"}
        )
    assert unsupported.value.status_code == 422
    assert unsupported.value.detail["code"] == "location_command_unsupported"

    with pytest.raises(HTTPException) as state:
        api.control_location_session(
            "session-1", {"owner": "run:R1", "command": "pause"}
        )
    assert state.value.status_code == 409
    assert state.value.detail["code"] == "location_control_state_conflict"


def test_unified_session_api_starts_and_controls_managed_route(monkeypatch) -> None:
    coordinator = _route_coordinator()
    monkeypatch.setattr(api, "location_session_coordinator", coordinator)

    started = api.create_location_session({"spec": _route_spec()})
    assert started["state"] == "running"
    assert started["source_runtime"]["route_id"] == ROUTE_ID

    scaled = api.control_location_session(
        "route-session-1",
        {"owner": "run:R1", "command": "set_time_scale", "value": 2},
    )
    assert scaled["source_runtime"]["time_scale"] == 2

    with pytest.raises(HTTPException) as missing:
        api.control_location_session(
            "route-session-1",
            {"owner": "run:R1", "command": "set_time_scale"},
        )
    assert missing.value.status_code == 422
    assert missing.value.detail["code"] == "location_control_value_required"

    held = api.control_location_session(
        "route-session-1", {"owner": "run:R1", "command": "hold"}
    )
    assert held["state"] == "holding"
    api.control_location_session(
        "route-session-1", {"owner": "run:R1", "command": "resume"}
    )
    arrived = api.control_location_session(
        "route-session-1", {"owner": "run:R1", "command": "arrive_now"}
    )
    assert arrived["state"] == "holding"
    assert arrived["source_runtime"]["remaining_m"] == 0


def test_location_route_catalog_exposes_tracked_summaries() -> None:
    payload = api.list_location_routes()

    assert payload["routes"]
    route = next(item for item in payload["routes"] if item["route_id"] == ROUTE_ID)
    assert route["total_m"] > 0
    assert set(route["origin"]) == {"lat", "lng"}
    assert set(route["destination"]) == {"lat", "lng"}


def test_location_checkpoint_catalog_and_explicit_paused_restore(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocationCheckpointStore(tmp_path / "checkpoints.json")

    def target_factory(
        _session: LocationSessionSpec,
        _target: LocationTargetSpec,
        _replace: bool,
    ) -> FakeLocationTargetAdapter:
        return FakeLocationTargetAdapter(clock=lambda: 10.0)

    def source_factory(session, emit):
        return RouteLocationSourceDriver(
            session,
            emit,
            run_async=False,
            checkpoint_store=store,
        )

    first = LocationSessionCoordinator(
        target_factory,
        source_driver_factory=source_factory,
    )
    first.start(LocationSessionSpec.from_mapping(_route_spec()))
    first.control("route-session-1", "pause", owner="run:R1")
    first.shutdown(preserve_checkpoints=True)

    restored_coordinator = LocationSessionCoordinator(
        target_factory,
        source_driver_factory=source_factory,
    )
    monkeypatch.setattr(api, "location_checkpoint_store", store)
    monkeypatch.setattr(api, "location_session_coordinator", restored_coordinator)

    catalog = api.list_location_checkpoints()
    assert catalog["checkpoints"][0]["session_id"] == "route-session-1"
    restored = api.restore_location_checkpoint(
        "route-session-1", {"owner": "run:R1"}
    )
    assert restored["state"] == "paused"

    with pytest.raises(HTTPException) as conflict:
        api.restore_location_checkpoint(
            "route-session-1", {"owner": "run:other"}
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "location_owner_conflict"


def test_unified_route_tail_api_round_trip_and_arrival(monkeypatch):
    coordinator = _route_coordinator()
    monkeypatch.setattr(api, "location_session_coordinator", coordinator)
    spec = _route_spec()
    destination = {"latitude": 31.2390, "longitude": 121.4810, "coordinate_system": "gcj02"}
    spec["source"]["tail_to"] = destination
    try:
        started = api.create_location_session({"spec": spec})
        assert started["spec"]["source"]["tail_to"] == destination
        assert started["source_runtime"]["tail_total_m"] > 0
        arrived = api.control_location_session(
            spec["session_id"], {"owner": spec["owner"], "command": "arrive_now"}
        )["source_runtime"]
        assert arrived["phase"] == "hold"
        assert arrived["current_coordinate"] == destination
        assert arrived["tail_distance_m"] == arrived["tail_total_m"]
        assert arrived["route_distance_m"] == arrived["total_m"]
    finally:
        coordinator.stop(spec["session_id"], owner=spec["owner"])


def test_unified_route_tail_api_rejects_coordinate_mismatch(monkeypatch):
    monkeypatch.setattr(api, "location_session_coordinator", _route_coordinator())
    spec = _route_spec()
    spec["source"]["tail_to"] = {
        "latitude": 31.2390, "longitude": 121.4810, "coordinate_system": "wgs84",
    }
    with pytest.raises(HTTPException) as rejected:
        api.create_location_session({"spec": spec})
    assert rejected.value.status_code == 422
    assert rejected.value.detail["code"] == "location_contract_invalid"


@pytest.mark.parametrize(("lock", "status", "code"), [
    (None, 503, "location_access_status_unavailable"),
    ({"ok": False}, 503, "location_access_status_unavailable"),
    ({"locked": True, "run_id": "another-owner"}, 409, "location_device_locked"),
])
def test_create_requires_confirmed_screen_access(monkeypatch, lock, status, code):
    def read_lock(device):
        assert device == "vphone-0"
        if lock is None:
            raise ConnectionError("Rig unavailable")
        return lock

    coordinator = LocationSessionCoordinator(
        lambda *_: pytest.fail("unconfirmed access reached the target"), lock_status_fn=read_lock,
    )
    monkeypatch.setattr(api, "location_session_coordinator", coordinator)
    with pytest.raises(HTTPException) as caught:
        api.create_location_session({"spec": _spec()})
    assert caught.value.status_code == status
    assert caught.value.detail["code"] == code
    assert api.list_location_sessions()["sessions"] == []
