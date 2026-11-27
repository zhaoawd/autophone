"""System location transport bound to a selected device socket."""
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass
class UDSSystemLocationTransport:
    sock_path: str | None = None
    timeout_s: float = 15.0

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        from autophone.device.target import resolve_protocol, resolve_target
        from autophone.device.uds import CommandNotAdmitted, request

        socket = self.sock_path or str(resolve_target().sock)
        if payload.get("t") in {"location_source_set", "location_stream_start"} and resolve_protocol(socket) == "upstream_v1":
            # The host can claim a generation before the guest rejects a disabled service.
            # A guest with services enabled may still have no current fix.
            try:
                probe, _ = request({"t": "location_current", "timeout": 0.25},
                                   sock_path=socket, timeout=self.timeout_s, parse_image=False)
            except CommandNotAdmitted:
                # Older guests may provide owned location without point readback.
                pass
            else:
                if probe.get("ok") is False:
                    failure = probe.get("guest_error") or probe
                    if isinstance(failure, dict) and isinstance(failure.get("error"), dict):
                        failure = failure["error"]
                    if isinstance(failure, dict) and failure.get("code") == "location_services_off":
                        return {**probe, "code": "location_services_off",
                                "error": failure.get("message") or probe.get("error") or "Location Services are off"}
        response, _sample = request(
            dict(payload),
            sock_path=socket,
            timeout=self.timeout_s,
            parse_image=False,
        )
        return response
