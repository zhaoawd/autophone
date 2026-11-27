from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from autophone.location import route_replay
from autophone.location.route_replay import (
    RouteFixtureError,
    RouteReplayEngine,
    angular_difference_deg,
    load_route_fixture,
    select_builtin_route_for_store,
    validate_route_fixture,
)


def _raw_route(
    coordinates: list[list[float]] | None = None,
    *,
    route_id: str = "test-route",
) -> dict:
    coords = coordinates or [
        [118.800000, 32.000000],
        [118.800000, 31.999000],
        [118.800000, 31.998000],
        [118.801000, 31.998000],
    ]
    return {
        "schema": "autophone.route-fixture/v1",
        "route_id": route_id,
        "coordinate_system": "GCJ-02",
        "travel_mode": "driving",
        "origin": {"lat": coords[0][1], "lng": coords[0][0]},
        "destination": {
            "name": "test destination",
            "lat": coords[-1][1],
            "lng": coords[-1][0],
        },
        "geometry": {"type": "LineString", "coordinates": coords},
        "maneuvers": [],
        "source": {"provider": "test", "captured_at": "2026-07-24"},
    }


def test_validate_route_fixture_builds_strict_cumulative_distance() -> None:
    route = validate_route_fixture(_raw_route())

    assert route.route_id == "test-route"
    assert route.total_m > 300
    assert route.cumulative_m[0] == 0
    assert all(
        left < right
        for left, right in zip(route.cumulative_m, route.cumulative_m[1:])
    )
    assert route.points[0].lat == pytest.approx(32.0)
    assert route.points[-1].lng == pytest.approx(118.801)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "wrong", "schema"),
        ("coordinate_system", "BD-09", "coordinate_system"),
        ("travel_mode", "walking", "travel_mode"),
    ],
)
def test_validate_route_fixture_rejects_wrong_contract(
    field: str, value: str, message: str,
) -> None:
    raw = _raw_route()
    raw[field] = value

    with pytest.raises(RouteFixtureError, match=message):
        validate_route_fixture(raw)


def test_validate_route_fixture_rejects_invalid_coordinate() -> None:
    raw = _raw_route()
    raw["geometry"]["coordinates"][1][0] = math.inf

    with pytest.raises(RouteFixtureError, match="finite"):
        validate_route_fixture(raw)


def test_validate_route_fixture_rejects_endpoint_mismatch() -> None:
    raw = _raw_route()
    raw["origin"] = {"lat": 31.0, "lng": 118.0}

    with pytest.raises(RouteFixtureError, match="origin"):
        validate_route_fixture(raw)


def test_validate_route_fixture_filters_small_duplicate_count() -> None:
    raw = _raw_route([
        [118.800000, 32.000000],
        [118.800000, 31.999000],
        [118.800000, 31.999000],
        [118.800000, 31.998000],
        [118.801000, 31.998000],
        [118.802000, 31.998000],
    ])

    route = validate_route_fixture(raw)

    assert len(route.points) == 5
    assert all(
        left < right
        for left, right in zip(route.cumulative_m, route.cumulative_m[1:])
    )


def test_validate_route_fixture_rejects_abnormal_duplicate_ratio() -> None:
    raw = _raw_route([
        [118.800000, 32.000000],
        [118.800000, 32.000000],
        [118.800000, 32.000000],
        [118.800000, 31.999000],
        [118.800000, 31.998000],
    ])

    with pytest.raises(RouteFixtureError, match="duplicate"):
        validate_route_fixture(raw)


def test_validate_route_fixture_rejects_large_geometry_jump() -> None:
    raw = _raw_route([
        [118.800000, 32.000000],
        [118.800000, 31.999000],
        [118.800000, 31.990000],
    ])

    with pytest.raises(RouteFixtureError, match="jump"):
        validate_route_fixture(raw)


def test_load_route_fixture_accepts_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "route.json"
    path.write_text(json.dumps(_raw_route()), encoding="utf-8")

    route = load_route_fixture(path)

    assert route.route_id == "test-route"


def test_load_route_fixture_rejects_unknown_builtin_and_path_escape() -> None:
    with pytest.raises(RouteFixtureError, match="unknown route fixture"):
        load_route_fixture("does-not-exist")
    with pytest.raises(RouteFixtureError, match="route id"):
        load_route_fixture("../escape")


def test_store_route_cache_selects_only_exact_store_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _raw_route(route_id="store-a-route")
    first["source"]["store_code"] = "STORE-A"
    second = _raw_route(route_id="store-b-route")
    second["source"]["store_code"] = "STORE-B"
    (tmp_path / "store-a-route.route.json").write_text(
        json.dumps(first),
        encoding="utf-8",
    )
    (tmp_path / "store-b-route.route.json").write_text(
        json.dumps(second),
        encoding="utf-8",
    )
    monkeypatch.setattr(route_replay, "BUILTIN_ROUTE_DIR", tmp_path)

    selected = select_builtin_route_for_store("STORE-B")

    assert selected.route_id == "store-b-route"


def test_store_route_cache_fails_closed_when_missing_or_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(route_replay, "BUILTIN_ROUTE_DIR", tmp_path)
    with pytest.raises(RouteFixtureError, match="no tracked route"):
        select_builtin_route_for_store("STORE-X")

    for route_id in ("store-x-one", "store-x-two"):
        raw = _raw_route(route_id=route_id)
        raw["source"]["store_code"] = "STORE-X"
        (tmp_path / f"{route_id}.route.json").write_text(
            json.dumps(raw),
            encoding="utf-8",
        )

    with pytest.raises(RouteFixtureError, match="multiple tracked routes"):
        select_builtin_route_for_store("STORE-X")


def test_tracked_example_route_has_self_checking_summary() -> None:
    route = load_route_fixture("example-demo-route-1p8km-driving")
    summary = route.source["route_summary"]

    assert route.total_m == pytest.approx(summary["geometry_distance_m"], abs=1.0)
    assert route.source["provider"] == "synthetic-example"
    assert summary["provider_step_count"] == len(route.maneuvers)
    assert 1_500 <= route.total_m <= 2_200


def test_sample_at_distance_interpolates_and_clamps() -> None:
    route = validate_route_fixture(_raw_route())

    start = route.sample_at_distance(-10)
    midpoint = route.sample_at_distance(route.cumulative_m[1] / 2)
    end = route.sample_at_distance(route.total_m + 10)

    assert (start.lat, start.lng) == pytest.approx((32.0, 118.8))
    assert midpoint.lat == pytest.approx(31.9995, abs=1e-6)
    assert midpoint.lng == pytest.approx(118.8, abs=1e-6)
    assert (end.lat, end.lng) == pytest.approx((31.998, 118.801))


def test_angular_difference_wraps_zero_degrees() -> None:
    assert angular_difference_deg(359, 1) == pytest.approx(2)
    assert angular_difference_deg(1, 359) == pytest.approx(-2)


def test_route_engine_propose_does_not_advance_until_commit() -> None:
    engine = RouteReplayEngine(validate_route_fixture(_raw_route()), time_scale=1)

    proposal = engine.propose(1.0)

    assert proposal.sample.route_distance_m > 0
    assert engine.current_sample().route_distance_m == 0
    engine.commit(proposal)
    assert engine.current_sample().route_distance_m == pytest.approx(
        proposal.sample.route_distance_m
    )
    with pytest.raises(RuntimeError, match="stale"):
        engine.commit(proposal)


def test_route_engine_accelerates_and_respects_cruise_speed() -> None:
    engine = RouteReplayEngine(validate_route_fixture(_raw_route()), time_scale=1)

    first = engine.propose(1.0)
    engine.commit(first)
    assert first.sample.model_speed_mps == pytest.approx(1.5, abs=0.05)

    later = engine.propose(20.0)
    assert 0 < later.sample.model_speed_mps <= 12.0


def test_route_engine_time_scale_scales_simulated_clock() -> None:
    route = validate_route_fixture(_raw_route())
    realtime = RouteReplayEngine(route, time_scale=1)
    accelerated = RouteReplayEngine(route, time_scale=10)

    sample_1x = realtime.propose(1.0).sample
    sample_10x = accelerated.propose(0.1).sample

    assert sample_10x.route_distance_m == pytest.approx(
        sample_1x.route_distance_m, rel=1e-6
    )
    assert sample_10x.model_speed_mps == pytest.approx(
        sample_1x.model_speed_mps, rel=1e-6
    )
    assert sample_10x.emitted_speed_mps == pytest.approx(
        sample_10x.model_speed_mps * 10
    )


def test_route_engine_curve_limit_is_lower_than_straight_limit() -> None:
    route = validate_route_fixture(_raw_route())
    engine = RouteReplayEngine(route)

    straight_limit = engine.curve_speed_limit_mps(30)
    corner_limit = engine.curve_speed_limit_mps(route.cumulative_m[2])

    assert corner_limit < straight_limit
    assert straight_limit == pytest.approx(12.0)


def test_route_engine_brakes_to_zero_at_exact_destination() -> None:
    engine = RouteReplayEngine(validate_route_fixture(_raw_route()), time_scale=10)

    final = engine.propose(100.0)
    engine.commit(final)
    sample = engine.current_sample()

    assert sample.done is True
    assert sample.route_distance_m == pytest.approx(sample.total_m)
    assert sample.remaining_m == 0
    assert sample.model_speed_mps == 0
    assert sample.emitted_speed_mps == 0


def test_route_engine_pause_resume_and_time_scale_keep_progress() -> None:
    engine = RouteReplayEngine(validate_route_fixture(_raw_route()), time_scale=10)
    moved = engine.propose(1.0)
    engine.commit(moved)
    distance = engine.current_sample().route_distance_m

    engine.pause()
    held = engine.propose(5.0)
    assert held.sample.route_distance_m == pytest.approx(distance)
    assert held.sample.emitted_speed_mps == 0
    engine.commit(held)

    engine.set_time_scale(1)
    assert engine.current_sample().route_distance_m == pytest.approx(distance)
    engine.resume()
    resumed = engine.propose(1.0)
    assert resumed.sample.route_distance_m > distance
    assert resumed.sample.time_scale == 1


def test_route_engine_propose_arrival_is_transactional() -> None:
    engine = RouteReplayEngine(validate_route_fixture(_raw_route()))

    arrival = engine.propose_arrival()

    assert arrival.sample.done is True
    assert engine.current_sample().done is False
    engine.commit(arrival)
    assert engine.current_sample().done is True


def test_route_tail_is_separate_transactional_progress_and_reaches_target():
    fixture = validate_route_fixture(_raw_route())
    destination = route_replay.RoutePoint(31.999, 118.802)
    engine = RouteReplayEngine(fixture, tail_to=destination, time_scale=1)
    route_end = engine.propose(1000)
    assert route_end.sample.phase == "route"
    assert route_end.sample.route_distance_m == fixture.total_m
    assert route_end.sample.done is False
    engine.commit(route_end)
    proposed = engine.propose(1)
    assert proposed.sample.phase == "tail"
    assert proposed.sample.tail_distance_m == 12
    assert proposed.sample.route_distance_m == fixture.total_m
    assert engine.current_sample().tail_distance_m == 0
    engine.commit(proposed)
    engine.pause()
    assert engine.propose(100).sample.tail_distance_m == 12
    engine.resume()
    engine.set_time_scale(2)
    assert engine.propose(1).sample.tail_distance_m == 36
    end = engine.commit(engine.propose(1000))
    assert end.done and end.emitted_speed_mps == 0
    assert (end.lat, end.lng) == (destination.lat, destination.lng)
    assert end.route_distance_m == fixture.total_m
    assert end.tail_distance_m == end.tail_total_m


def test_route_tail_checkpoint_restores_paused_and_rejects_other_destination():
    fixture = validate_route_fixture(_raw_route())
    tail = route_replay.RoutePoint(31.999, 118.802)
    engine = RouteReplayEngine(fixture, tail_to=tail, time_scale=1)
    engine.commit(engine.propose(1000))
    engine.commit(engine.propose(1))
    saved = engine.checkpoint()
    restored = RouteReplayEngine(fixture, tail_to=tail)
    sample = restored.restore_checkpoint(saved)
    assert restored.paused
    assert sample.tail_distance_m == 12
    assert sample.emitted_speed_mps == 0
    assert restored.propose(100).sample.tail_distance_m == 12
    with pytest.raises(ValueError, match="tail_to mismatch"):
        RouteReplayEngine(fixture).restore_checkpoint(saved)
    with pytest.raises(ValueError, match="completed route"):
        restored.restore_checkpoint({**saved, "route_distance_m": 0})
    with pytest.raises(ValueError, match="tail bounds"):
        restored.restore_checkpoint({**saved, "tail_distance_m": 1e6})


def test_zero_length_tail_and_arrive_now():
    fixture = validate_route_fixture(_raw_route())
    engine = RouteReplayEngine(fixture, tail_to=fixture.points[-1])
    assert engine.commit(engine.propose(1000)).done
    assert engine.current_sample().tail_total_m == 0
    engine = RouteReplayEngine(fixture, tail_to=route_replay.RoutePoint(31.999, 118.802))
    sample = engine.arrive_now()
    assert sample.done and sample.phase == "tail"
    assert sample.tail_distance_m == sample.tail_total_m
    assert (sample.lat, sample.lng) == (31.999, 118.802)


def test_legacy_route_checkpoint_without_tail_remains_readable():
    fixture = validate_route_fixture(_raw_route())
    engine = RouteReplayEngine(fixture)
    saved = engine.checkpoint()
    saved.pop("tail_to")
    saved.pop("tail_distance_m")
    assert engine.restore_checkpoint(saved).route_distance_m == 0


def test_builtin_route_catalog_refuses_symlinks(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    external = tmp_path / "fixture.json"
    external.write_text(json.dumps(_raw_route()))
    (catalog / "test-route.route.json").symlink_to(external)
    monkeypatch.setattr(route_replay, "BUILTIN_ROUTE_DIR", catalog)
    with pytest.raises(RouteFixtureError):
        load_route_fixture("test-route")
    with pytest.raises(RouteFixtureError):
        route_replay.list_builtin_routes()
