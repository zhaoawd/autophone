from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Lock
import time

import pytest

from autophone.location.contracts import (
    LocationCoordinate,
    LocationSessionSpec,
    LocationSourceSpec,
    LocationTargetSpec,
)
from autophone.location.sources import (
    ApproachLocationSourceDriver,
    LocationSourceError,
    RouteLocationSourceDriver,
)


ROUTE_ID = "example-demo-route-1p8km-driving"


def _session() -> LocationSessionSpec:
    return LocationSessionSpec(
        session_id="route-session-1",
        owner="run:R1",
        device="vphone-0",
        source=LocationSourceSpec(
            source_id=f"route:{ROUTE_ID}",
            kind="route",
            coordinate_system="gcj02",
            route_id=ROUTE_ID,
            time_scale=1,
            watchdog_s=3,
        ),
        targets=(LocationTargetSpec(
            target_id="system:vphone-0",
            kind="system_core_location",
            device="vphone-0",
            coordinate_system="wgs84",
        ),),
    )


def _approach_session() -> LocationSessionSpec:
    return LocationSessionSpec(
        session_id="approach-session-1",
        owner="run:R1",
        device="vphone-0",
        source=LocationSourceSpec(
            source_id="approach:delivery",
            kind="approach",
            coordinate_system="gcj02",
            origin=LocationCoordinate(31.2300, 121.4700, "gcj02"),
            destination=LocationCoordinate(31.2380, 121.4800, "gcj02"),
            speed_mps=7,
            min_distance_m=20,
        ),
        targets=(LocationTargetSpec(
            target_id="system:vphone-0",
            kind="system_core_location",
            device="vphone-0",
            coordinate_system="wgs84",
        ),),
    )


def _driver(emit, **kwargs) -> RouteLocationSourceDriver:
    return RouteLocationSourceDriver(
        _session(),
        emit,
        wall_clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        monotonic=lambda: 10.0,
        run_async=False,
        **kwargs,
    )


def test_route_driver_starts_at_origin_and_exposes_controls() -> None:
    samples = []
    driver = _driver(lambda sample: samples.append(sample) or True)

    started = driver.start()
    scaled = driver.control("set_time_scale", value=2)
    paused = driver.control("pause")
    resumed = driver.control("resume")
    held = driver.control("hold")
    resumed_from_hold = driver.control("resume")
    arrived = driver.control("arrive_now")

    assert started.state == "running"
    assert started.last_producer_sequence == 0
    assert samples[0].producer_sequence == 0
    assert samples[0].coordinate.coordinate_system == "gcj02"
    assert scaled.time_scale == 2
    assert paused.state == "paused"
    assert resumed.state == "running"
    assert held.state == "holding"
    assert resumed_from_hold.state == "running"
    assert arrived.state == "holding"
    assert arrived.last_producer_sequence == 1
    assert arrived.route_distance_m == arrived.total_m
    assert arrived.remaining_m == 0
    assert samples[-1].speed_mps == 0


def test_route_driver_background_clock_continuously_emits_moving_samples() -> None:
    samples = []
    samples_mu = Lock()
    received_three = Event()

    def emit(sample) -> bool:
        with samples_mu:
            samples.append(sample)
            if len(samples) >= 3:
                received_three.set()
        return True

    driver = RouteLocationSourceDriver(_session(), emit, tick_s=0.01)
    driver.start()
    try:
        assert received_three.wait(1.0)
    finally:
        driver.stop()

    with samples_mu:
        captured = tuple(samples)
    assert [sample.producer_sequence for sample in captured[:3]] == [0, 1, 2]
    assert captured[1].coordinate != captured[0].coordinate
    assert captured[2].coordinate != captured[1].coordinate


def test_route_driver_resume_retries_pending_sample_after_retry_exhaustion() -> None:
    allow_retry = Event()
    failed_twice = Event()
    recovered = Event()
    failure_count = 0
    seen_sequences = []
    seen_mu = Lock()

    def emit(sample) -> bool:
        nonlocal failure_count
        with seen_mu:
            seen_sequences.append(sample.producer_sequence)
        if sample.producer_sequence == 0:
            return True
        if allow_retry.is_set():
            recovered.set()
            return True
        failure_count += 1
        if failure_count >= 2:
            failed_twice.set()
        return False

    driver = RouteLocationSourceDriver(
        _session(), emit, tick_s=0.01, max_consecutive_failures=2
    )
    driver.start()
    try:
        assert failed_twice.wait(1.0)
        deadline = time.monotonic() + 1.0
        while driver.snapshot().state != "error_paused" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert driver.snapshot().state == "error_paused"

        allow_retry.set()
        assert driver.control("resume").state == "running"
        assert recovered.wait(1.0)
    finally:
        driver.stop()

    with seen_mu:
        assert seen_sequences[:4] == [0, 1, 1, 1]


def test_approach_driver_starts_at_origin_and_stops_at_minimum_distance() -> None:
    samples = []
    session = _approach_session()
    driver = ApproachLocationSourceDriver(
        session,
        lambda sample: samples.append(sample) or True,
        wall_clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        monotonic=lambda: 10.0,
        run_async=False,
    )

    started = driver.start()
    scaled = driver.control("set_time_scale", value=2)
    arrived = driver.control("arrive_now")

    assert started.source_kind == "approach"
    assert started.traveled_m == 0
    assert samples[0].coordinate == session.source.origin
    assert scaled.time_scale == 2
    assert arrived.state == "holding"
    assert arrived.remaining_m == pytest.approx(20)
    assert arrived.traveled_m == pytest.approx(arrived.total_m - 20)
    assert samples[-1].speed_mps == 0


def test_route_driver_freezes_rejected_arrival_sample() -> None:
    accepted = iter((True, False))
    driver = _driver(lambda _sample: next(accepted))
    driver.start()

    with pytest.raises(LocationSourceError) as rejected:
        driver.control("arrive_now")
    assert rejected.value.code == "location_delivery_rejected"
    assert driver.snapshot().state == "backoff"

    with pytest.raises(LocationSourceError) as pending:
        driver.control("arrive_now")
    assert pending.value.code == "location_source_pending_sample"


def test_route_driver_rejects_invalid_controls_and_stops_idempotently() -> None:
    driver = _driver(lambda _sample: True)
    driver.start()

    with pytest.raises(LocationSourceError) as missing:
        driver.control("set_time_scale")
    assert missing.value.code == "location_control_value_required"

    with pytest.raises(LocationSourceError) as invalid:
        driver.control("set_time_scale", value=0)
    assert invalid.value.code == "location_control_value_invalid"

    stopped = driver.stop()
    assert stopped.state == "stopped"
    assert driver.stop().state == "stopped"


def test_route_tail_rejected_sample_survives_pause_scale_and_checkpoint(tmp_path):
    from dataclasses import replace
    from autophone.location.checkpoints import LocationCheckpointStore

    original = _session()
    session = replace(original, source=replace(
        original.source, tail_to=LocationCoordinate(31.2390, 121.4810, "gcj02")
    ))
    accepted = True
    samples = []
    def emit(sample):
        samples.append(sample)
        return accepted
    store = LocationCheckpointStore(tmp_path / "checkpoints.json")
    driver = RouteLocationSourceDriver(session, emit, run_async=False, checkpoint_store=store)
    driver.start()
    route = driver.engine.propose(1000)
    assert driver._attempt(route, route.sample)
    assert driver.snapshot().state == "running"
    accepted = False
    tail = driver.engine.propose(1)
    assert not driver._attempt(tail, tail.sample)
    assert driver.snapshot().tail_distance_m == 0
    assert store.load(session)["tail_distance_m"] == 0
    driver.control("pause")
    driver.control("set_time_scale", value=2)
    driver.control("resume")
    accepted = True
    assert driver._retry_pending()
    assert samples[-1] == samples[-2]
    assert driver.snapshot().tail_distance_m == 12
    assert driver.snapshot().phase == "tail"
    assert driver.snapshot().traveled_m == driver.snapshot().total_m + 12
    driver.stop(preserve_checkpoint=True)
    recovered = RouteLocationSourceDriver(session, emit, run_async=False, checkpoint_store=store)
    snapshot = recovered.start()
    assert snapshot.state == "paused"
    assert snapshot.tail_distance_m == 12
    assert snapshot.emitted_speed_mps == 0
    recovered.control("resume")
    arrived = recovered.control("arrive_now")
    assert arrived.state == "holding" and arrived.phase == "hold"
    assert arrived.tail_distance_m == arrived.tail_total_m
    assert arrived.current_coordinate == session.source.tail_to
    recovered.stop()
