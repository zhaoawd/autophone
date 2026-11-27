"""Opt-in two-Rig acceptance through the candidate dashboard and its Control Plane."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from autophone.location.journey.coordinates import gcj02_to_wgs84
from autophone.location.route_replay import haversine_m, load_route_fixture


@pytest.mark.live_device
def test_two_guests_fixed_and_route_readback(tmp_path):
    base = os.getenv("AUTOPHONE_LIVE_DASHBOARD")
    rigs = os.getenv("AUTOPHONE_LIVE_LOCATION_RIGS", "").split(",")
    if not base or not all(rigs):
        pytest.skip("set AUTOPHONE_LIVE_DASHBOARD and AUTOPHONE_LIVE_LOCATION_RIGS=a,b")
    assert len(rigs) == 2 and len(set(rigs)) == 2
    owner = "platform-live-" + uuid.uuid4().hex
    route = load_route_fixture("example-demo-route-1p8km-driving")
    endpoint = gcj02_to_wgs84(route.destination.lat, route.destination.lng)
    points = dict(zip(rigs, [(37.332, -122.031), (48.858, 2.294)]))
    opener = build_opener(ProxyHandler({}))
    evidence = tmp_path / "guest-readbacks.jsonl"

    def api(path, body=None, method=None):
        request = Request(base.rstrip("/") + path, method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=40) as response:
            return json.load(response)

    @contextmanager
    def session(rig, kind):
        sid = uuid.uuid4().hex
        source = {"kind": kind, "source_id": sid, "coordinate_system": "wgs84" if kind == "fixed" else "gcj02"}
        body = {"spec": {"device": rig, "owner": owner, "session_id": sid, "source": source,
                         "targets": [{"kind": "system_core_location", "target_id": "system:" + rig,
                                      "coordinate_system": "wgs84"}]}}
        if kind == "fixed":
            lat, lng = points[rig]
            body["initial_sample"] = {
                "producer_sequence": 0, "emitted_wall_at": datetime.now(timezone.utc).isoformat(),
                "emitted_monotonic_s": time.monotonic(),
                "coordinate": {"latitude": lat, "longitude": lng, "coordinate_system": "wgs84"},
            }
        else:
            source.update(route_id=route.route_id, time_scale=8, watchdog_s=5)
        api("/api/location/sessions", body)
        try:
            yield sid
        finally:
            stopped = api("/api/location/sessions/" + sid, {"owner": owner}, "DELETE")
            assert stopped["state"] == "stopped", stopped

    def read(rig, sid):
        for attempt in range(5):
            try:
                result = api("/api/location/devices/" + quote(rig, safe="") + "/current")
                break
            except HTTPError as error:
                if attempt == 4 or error.code != 409 or json.load(error).get("detail", {}).get("code") != "location_device_locked":
                    raise
                time.sleep(.2)
        assert result["device"] == rig
        sample = result["reading"]
        with evidence.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"session_id": sid, **result}) + "\n")
        assert sample["ok"] and sample["fresh"] and sample["simulated"] and sample["timestamp"]
        return (sample["latitude"], sample["longitude"]), sample["timestamp"]

    for active, other in (rigs, rigs[::-1]):
        with session(other, "fixed") as reference:
            with session(active, "fixed") as fixed:
                for rig, sid in ((active, fixed), (other, reference)):
                    assert haversine_m(*read(rig, sid)[0], *points[rig]) < 1
            with session(active, "route") as replay:
                timestamps, locations = set(), []
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    pos, stamp = read(active, replay)
                    timestamps.add(stamp)
                    locations.append(pos)
                    assert haversine_m(*read(other, reference)[0], *points[other]) < 1
                    if haversine_m(*pos, *endpoint) < 1:
                        break
                    time.sleep(2)
                assert len(timestamps) >= 3 and haversine_m(*locations[0], *locations[-1]) > 100
                assert haversine_m(*locations[-1], *endpoint) < 1
