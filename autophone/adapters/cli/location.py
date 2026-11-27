"""System location commands routed through the authenticated dashboard API."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
import sys
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx2

from autophone.location.contracts import LocationCoordinate


def dashboard_url(value: str) -> str:
    url = urlsplit(value)
    if not value.strip() or any(c in value for c in ("\r", "\n", "\t", "\\")):
        raise ValueError("provide a valid dashboard URL")
    if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None:
        raise ValueError("provide an HTTP(S) dashboard URL without credentials")
    if url.path not in {"", "/"} or url.query or url.fragment:
        raise ValueError("provide the dashboard origin without a path, query or fragment")
    if url.scheme == "http":
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if not loopback:
            raise ValueError("a remote dashboard requires HTTPS")
    return value.rstrip("/")


def location_request(args: argparse.Namespace, client: httpx2.Client) -> dict:
    rig = args.rig.strip()
    if not rig:
        raise ValueError("--rig is required")
    device = quote(rig, safe="")
    if args.action == "current":
        response = client.get(f"/api/location/devices/{device}/current")
    elif args.action == "stop":
        path = "/api/location/sessions/" + quote(args.session_id, safe="")
        before = client.get(path)
        before.raise_for_status()
        if before.json().get("spec", {}).get("device") != rig:
            raise ValueError("the session belongs to a different Rig")
        response = client.request("DELETE", path, json={"owner": args.owner})
    else:
        session_id = args.session_id or f"cli-{uuid4().hex}"
        source = {"source_id": session_id}
        spec = {"session_id": session_id, "device": rig, "owner": args.owner, "source": source,
                "targets": [{"kind": "system_core_location", "target_id": "system:" + rig,
                             "coordinate_system": "wgs84"}]}
        body = {"spec": spec}
        if args.action == "route":
            source.update(kind="route", coordinate_system="gcj02", route_id=args.route_id)
        else:
            coord = LocationCoordinate(latitude=args.latitude, longitude=args.longitude, coordinate_system="wgs84")
            source.update(kind="fixed", coordinate_system="wgs84")
            body["initial_sample"] = {
                "producer_sequence": 0, "emitted_wall_at": datetime.now(timezone.utc).isoformat(),
                "emitted_monotonic_s": time.monotonic(), "coordinate": coord.as_dict(),
            }
        response = client.post("/api/location/sessions", json=body)
    response.raise_for_status()
    return response.json()


def cmd_location(args: argparse.Namespace) -> int:
    try:
        base = dashboard_url(args.dashboard)
        credentials = os.getenv("AUTOPHONE_DASHBOARD_AUTH")
        auth = tuple(credentials.split(":", 1)) if credentials else None
        if auth is not None and (len(auth) != 2 or not all(auth)):
            raise ValueError("AUTOPHONE_DASHBOARD_AUTH must contain a username and password")
        with httpx2.Client(base_url=base, auth=auth, timeout=30, trust_env=False) as client:
            print(json.dumps(location_request(args, client), ensure_ascii=False))
    except httpx2.HTTPStatusError as exc:
        print(f"HTTP {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 1
    except (httpx2.RequestError, ValueError) as exc:
        print(f"location: {exc}", file=sys.stderr)
        return 2
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    location = sub.add_parser("location", help="manage a Rig's system location through the dashboard")
    location.add_argument("--dashboard", default="http://127.0.0.1:8000")
    commands = location.add_subparsers(dest="action", required=True)
    for name in ("current", "set", "route", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--rig", required=True)
        if name != "current":
            command.add_argument("--owner", required=True)
            command.add_argument("--session-id", required=name == "stop")
        if name == "set":
            command.add_argument("--latitude", type=float, required=True)
            command.add_argument("--longitude", type=float, required=True)
        if name == "route":
            command.add_argument("route_id", help="built-in route ID")
        command.set_defaults(func=cmd_location)
