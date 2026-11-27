"""Wire adaptation only: callers retain their logical operation and permission class."""
from __future__ import annotations

import base64
import math
from contextlib import contextmanager
from pathlib import Path
import tempfile

from . import upstream_location

RPC = {
    "app_list": "apps.list", "app_foreground": "apps.foreground",
    "app_launch": "apps.launch", "app_terminate": "apps.terminate",
    "open_url": "apps.open_url", "shell": "shell.exec", "ipa_install": "apps.install",
    "location": "location.set", "location_current": "location.current",
    "location_stop": "location.clear",
}
DIRECT = {"ping", "capabilities", "tap", "swipe", "key", "type"}
LOCATION_COMMANDS = frozenset({"location", "location_current", "location_stop"})
INLINE_LIMIT = 512 * 1024


def admitted(capabilities: dict, kind: str) -> bool:
    commands = capabilities.get("commands")
    if (capabilities.get("ok") is not True or capabilities.get("protocol") != "upstream_v1"
            or not isinstance(commands, dict)):
        return False
    if kind in upstream_location.LOCATION:
        guest = capabilities.get('guest_capabilities')
        return (capabilities.get('guest_connected') is True and commands.get(kind) is True
                and isinstance(guest, list) and "location_owned" in guest)
    if kind in LOCATION_COMMANDS:
        guest = capabilities.get("guest_capabilities")
        return (capabilities.get("guest_connected") is True and commands.get("rpc") is True
                and commands.get(kind, True) is True and isinstance(guest, list)
                and "location" in guest)
    if kind == "ipa_install":
        guest = capabilities.get("guest_capabilities")
        return (commands.get("ipa_install", True) is True and commands.get("rpc") is True
                and capabilities.get("guest_connected") is True and isinstance(guest, list)
                and "ipa_install" in guest)
    return commands.get(kind) is True


def _location_number(value, field):
    if type(value) not in (int, float):
        raise ValueError(f"location {field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"location {field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"location {field} must be a finite number")
    return result


def _location_params(kind, values):
    allowed = ({"lat", "lon", "alt", "hacc", "vacc", "speed", "course"}
               if kind == "location" else {"timeout"} if kind == "location_current" else set())
    if set(values) - allowed:
        raise ValueError("unsupported location parameter")
    if kind == "location_stop":
        return {}
    if kind == "location_current":
        timeout = _location_number(values.get("timeout", 10), "timeout")
        if not 0 < timeout <= 300:
            raise ValueError("location timeout must be more than 0 and at most 300 seconds")
        return {"timeout": timeout}
    latitude = _location_number(values.get("lat"), "latitude")
    longitude = _location_number(values.get("lon"), "longitude")
    altitude = _location_number(values.get("alt", 0), "altitude")
    hacc = _location_number(values.get("hacc", 5), "horizontal accuracy")
    vacc = _location_number(values.get("vacc", 5), "vertical accuracy")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and abs(altitude) <= 100000
            and hacc >= 0 and (vacc >= 0 or vacc == -1)):
        raise ValueError("location coordinate or accuracy is outside the native range")
    params = {"latitude": latitude, "longitude": longitude, "altitude": altitude,
              "horizontal_accuracy": hacc, "vertical_accuracy": vacc}
    for field in ("speed", "course"):
        if field in values:
            number = _location_number(values[field], field)
            if number == -1:
                continue  # CoreLocation unknown is represented by native optional arguments.
            if number < 0 or (field == "course" and number >= 360):
                raise ValueError(f"location {field} is outside the native range")
            params[field] = number
    return params


def _validate_location_reading(value):
    if not isinstance(value, dict):
        raise ValueError("location readback must be an object")
    latitude = _location_number(value.get("latitude"), "readback latitude")
    longitude = _location_number(value.get("longitude"), "readback longitude")
    age = _location_number(value.get("age_seconds"), "readback age")
    if (not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or age < 0
            or type(value.get("fresh")) is not bool or type(value.get("simulated")) is not bool
            or not isinstance(value.get("timestamp"), str) or not value["timestamp"]):
        raise ValueError("location readback identity or freshness fields are invalid")


@contextmanager
def prepare(message: dict):
    """Yield (one wire message, response decoder). Never retry a write."""
    kind = message.get("t")
    if kind in RPC:
        params = {k: v for k, v in message.items() if k not in {"t", "screen", "delay"}}
        if kind in LOCATION_COMMANDS:
            params = _location_params(kind, params)
        wire = {"t": "rpc", "method": RPC[kind], "params": params,
                "screen": message.get("screen", False)}
        yield wire, lambda response: decode(kind, response, expected_location=params if kind == "location" else None)
    elif kind == "screenshot":
        yield {**message, "save": bool(message.get("path"))}, lambda response: response
    elif kind in upstream_location.LOCATION:
        wire = upstream_location.prepare(message)
        yield wire, lambda response: upstream_location.validate(kind, decode(kind, response), wire)
    elif kind in DIRECT:
        yield dict(message), lambda response: response
    elif kind in {"file_get", "file_put"}:
        path = message.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("file transfer requires an absolute guest path")
        with tempfile.TemporaryDirectory(prefix="autophone-wire-") as directory:
            scratch = Path(directory) / "payload"
            if kind == "file_put":
                local = message.get("load")
                if local is None:
                    data = message.get("data_b64")
                    if not isinstance(data, str) or len(data) > ((INLINE_LIMIT + 2) // 3) * 4:
                        raise ValueError("inline upload exceeds limit or lacks data_b64")
                    payload = base64.b64decode(data, validate=True)
                    if len(payload) > INLINE_LIMIT:
                        raise ValueError("inline upload exceeds limit")
                    scratch.write_bytes(payload)
                    local = str(scratch)
                local = str(Path(local).expanduser().resolve())
                mode = message.get("perm", "644")
                if not isinstance(mode, str) or not mode or any(c not in "01234567" for c in mode) or int(mode, 8) > 0o777:
                    raise ValueError("file permission must be an octal string up to 777")
                yield {"t": "push", "local": local, "path": path, "mode": mode}, lambda response: decode(kind, response)
            else:
                destination = message.get("save")
                local = Path(destination).expanduser().resolve() if destination else scratch
                def finish(response):
                    result = decode(kind, response)
                    if result.get("ok") is True and not destination:
                        if local.stat().st_size > INLINE_LIMIT:
                            raise ValueError("inline download exceeds limit; use save")
                        result["data_b64"] = base64.b64encode(local.read_bytes()).decode("ascii")
                    return result
                yield {"t": "pull", "path": path, "local": str(local)}, finish
    else:
        raise ValueError(f"unsupported upstream logical command: {kind}")


def decode(kind: str, response: dict, *, expected_location=None) -> dict:
    if response.get("ok") is not True:
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"{kind}: missing upstream result object")
    output = {**result, **{k: v for k, v in response.items() if k != "result"}}
    if kind == "location":
        location = result.get("location")
        _validate_location_reading(location)
        if result.get("simulating") is not True or location["fresh"] is not True or location["simulated"] is not True:
            raise ValueError("location set did not confirm a fresh simulated fix")
        if expected_location is not None and any(
            abs(location[field] - expected_location[field]) >= 1e-7
            for field in ("latitude", "longitude")
        ):
            raise ValueError("location readback differs from requested coordinates")
    elif kind == "location_current":
        _validate_location_reading(result)
    elif kind == "location_stop":
        if result.get("simulating") is not False:
            raise ValueError("location clear did not confirm simulation stopped")
        if "location" in result:
            _validate_location_reading(result["location"])
            if result["location"]["fresh"] and result["location"]["simulated"]:
                raise ValueError("location clear still reports a fresh simulated fix")
    if kind == "app_list":
        apps = result.get("apps")
        if not isinstance(apps, list) or not all(isinstance(a, dict) for a in apps):
            raise ValueError("apps.list returned invalid apps")
        output["apps"] = [{**a, **({"data_container": a["data_path"]} if "data_path" in a else {})} for a in apps]
    if kind == "app_launch":
        if type(result.get("pid")) is not int or result["pid"] <= 0 or result.get("frontmost_verified") is not True:
            output.update(ok=False, error="application launch/foreground was not verified", operation_may_continue=True)
    if kind == "shell":
        required = {"stdout": str, "stderr": str, "exit_code": int, "timed_out": bool,
                    "stdout_truncated": bool, "stderr_truncated": bool}
        if any(type(result.get(key)) is not value for key, value in required.items()):
            raise ValueError("shell.exec returned an invalid result")
        output["code"] = result["exit_code"]
        output["truncated"] = result["stdout_truncated"] or result["stderr_truncated"]
    return output
