"""Check a managed Rig for an iOS USB trust prompt and decline it if present."""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from unicodedata import normalize
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from autophone.application.device_session import (
    DeviceSessionRefused,
    DeviceSessions,
    RemoteDeviceSessions,
    control_http_base_url,
)
from autophone.device.ocr_target import run_ocr
from autophone.device.shell import ShellError, run_shell
from autophone.rig.target import DeviceTarget


def _prompt_labels(ocr: dict) -> list[tuple[str, dict]]:
    if (ocr.get("image_w"), ocr.get("image_h")) != (430, 932):
        raise RuntimeError("unexpected console frame dimensions")
    return [(normalize("NFKC", str(box["text"])).strip().casefold().replace("’", "'").rstrip("?"), box)
            for box in ocr["boxes"]]


def decline_position(ocr: dict) -> tuple[int, int] | None:
    labels = _prompt_labels(ocr)
    titles = [box for text, box in labels if text == "trust this computer"]
    buttons = [box for text, box in labels if text == "don't trust"]
    if len(titles) != 1 or len(buttons) != 1:
        return None
    title, button = titles[0], buttons[0]
    x = int(button["x"] + button["w"] / 2)
    y = int(button["y"] + button["h"] / 2)
    if 35 <= x <= 245 and 330 <= title["y"] < y <= 650:
        return x, y
    return None


def wait_for_device(base_url: str, target: DeviceTarget, *, timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urlopen(base_url.rstrip("/") + "/api/control/device-sessions", timeout=2) as response:
                devices = json.load(response)["devices"]
        except (URLError, TimeoutError):
            devices = []
        for device in devices:
            if device.get("device") != target.rig_id:
                continue
            identity = (device.get("host_id"), device.get("rig_id"), device.get("rig_generation"))
            if identity != target.identity:
                raise RuntimeError(f"managed device session identity differs: {identity!r}")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"managed device session unavailable for {target.rig_id}")
        time.sleep(0.5)


def _start_console_with_warmup(
    client: DeviceSessions,
    rig: str,
    *,
    warmup_s: float = 45.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Open a managed console session, tolerating transient 503 right after cold boot."""
    deadline = time.monotonic() + max(0.0, warmup_s)
    while True:
        try:
            return client.call(rig, "console_start", {"request_id": uuid4().hex})
        except DeviceSessionRefused as exc:
            if exc.status != 503 or time.monotonic() >= deadline:
                raise
            sleep_fn(min(1.0, max(0.0, deadline - time.monotonic())))


def dismiss(
    client: DeviceSessions,
    rig: str,
    output: Path,
    *,
    wait_s: float,
    console_warmup_s: float = 45.0,
    frame_retry_s: float = 15.0,
) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    session = _start_console_with_warmup(client, rig, warmup_s=console_warmup_s)
    params = {key: session[key] for key in ("session_id", "controller_token")}

    def capture() -> dict:
        retry_deadline = time.monotonic() + max(0.0, frame_retry_s)
        while True:
            try:
                return client.call(rig, "console_frame", params)
            except DeviceSessionRefused as exc:
                if exc.status != 503 or time.monotonic() >= retry_deadline:
                    raise
                time.sleep(min(0.5, max(0.0, retry_deadline - time.monotonic())))

    def event(frame, **intent):
        receipt = client.call(rig, "console_event", {
            **params, "event": {**intent, "event_id": uuid4().hex, "frame_seq": frame["seq"]},
        })
        if receipt.get("ok") is not True:
            raise RuntimeError(f"console event refused: {receipt}")
        return receipt

    try:
        for _ in range(2):
            event(capture(), kind="key", name="home")
            time.sleep(0.8)
        deadline = time.monotonic() + wait_s
        sample = 0
        while True:
            frame = capture()
            sample += 1
            image = output / f"check-{sample:02d}.jpg"
            image.write_bytes(base64.b64decode(frame["jpeg_b64"], validate=True))
            ocr = run_ocr(image)
            pos = decline_position(ocr)
            if pos is not None:
                receipt = event(frame, kind="tap", px=pos[0], py=pos[1])
                time.sleep(0.8)
                after = output / "after.jpg"
                after_frame = capture()
                after.write_bytes(base64.b64decode(after_frame["jpeg_b64"], validate=True))
                after_labels = {label for label, _ in _prompt_labels(run_ocr(after))}
                if after_frame["seq"] <= frame["seq"] or not after_labels or after_labels.intersection(
                        {"trust this computer", "don't trust"}):
                    raise RuntimeError("USB trust prompt disappearance could not be verified")
                return {"status": "declined", "frame": image.name, "after": after.name, "receipt": receipt}
            if time.monotonic() >= deadline:
                if any(label in {"trust this computer", "don't trust"} for label, _ in _prompt_labels(ocr)):
                    raise RuntimeError("USB trust prompt could not be matched safely")
                return {"status": "not_observed", "frames": sample}
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    finally:
        closed = client.call(rig, "console_stop", params)
        (output / "console-stop.json").write_text(json.dumps(closed, indent=2) + "\n")
        if closed.get("lifecycle") != "closed" or closed.get("error_reason"):
            raise RuntimeError("managed console close was not verified")


class DashboardDeviceSessions:
    """DeviceSessions adapter over a local dashboard's /api/devices/{rig}/console routes."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        timeout_s: float = 30.0,
        opener: Callable[[Request, float], Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s
        self._opener = opener or (lambda req, timeout: urlopen(req, timeout=timeout))
        self._tokens: dict[str, tuple[str, str]] = {}

    def _basic_headers(self) -> dict[str, str]:
        if not self.auth:
            return {}
        raw = f"{self.auth[0]}:{self.auth[1]}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[bytes, Any]:
        req_headers = dict(headers or {})
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(self.base_url + path, data=data, headers=req_headers, method=method)
        try:
            with self._opener(req, self.timeout_s) as response:
                return response.read(), response.headers
        except HTTPError as exc:
            detail: Any = None
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:  # noqa: BLE001
                detail = None
            if isinstance(detail, dict):
                raise DeviceSessionRefused(
                    str(detail.get("message") or exc.reason or "dashboard console refused"),
                    status=int(exc.code),
                    code=str(detail.get("code") or "device_session_error"),
                ) from exc
            raise DeviceSessionRefused(
                str(detail or exc.reason or "dashboard console refused"),
                status=int(exc.code),
                code="device_session_error",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DeviceSessionRefused(
                f"dashboard device console unreachable: {exc}",
                status=503,
                code="device_session_unavailable",
            ) from exc

    def _session_headers(self, session_id: str) -> dict[str, str]:
        pair = self._tokens.get(session_id)
        if pair is None:
            raise DeviceSessionRefused(
                f"missing console access token for session {session_id}",
                status=401,
                code="console_access_missing",
            )
        header_name, token = pair
        return {header_name: token}

    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        dev = quote(str(device), safe="")
        if op == "console_start":
            body, _ = self._request(
                "POST",
                f"/api/devices/{dev}/console",
                headers=self._basic_headers(),
                json_body={"request_id": payload.get("request_id")},
            )
            result = json.loads(body.decode("utf-8"))
            session_id = str(result.get("session_id") or "")
            token = str(result.get("access_token") or "")
            header_name = str(result.get("access_header") or "x-console-access-token")
            if not session_id or not token:
                raise RuntimeError(f"dashboard console_start missing session credentials: {result}")
            self._tokens[session_id] = (header_name, token)
            return {**result, "controller_token": token}

        session_id = str(payload.get("session_id") or "")
        sid = quote(session_id, safe="")
        headers = self._session_headers(session_id)

        if op == "console_frame":
            raw, resp_headers = self._request("GET", f"/api/devices/{dev}/console/{sid}/frame", headers=headers)
            seq = int(resp_headers.get("X-Frame-Seq") or 0)
            captured_at = float(resp_headers.get("X-Captured-At") or 0.0)
            return {
                "seq": seq,
                "captured_at": captured_at,
                "jpeg_b64": base64.b64encode(raw).decode("ascii"),
            }
        if op == "console_event":
            body, _ = self._request(
                "POST",
                f"/api/devices/{dev}/console/{sid}/event",
                headers=headers,
                json_body=dict(payload.get("event") or {}),
            )
            return json.loads(body.decode("utf-8"))
        if op == "console_stop":
            try:
                body, _ = self._request(
                    "POST",
                    f"/api/devices/{dev}/console/{sid}/stop",
                    headers=headers,
                    json_body={},
                )
            finally:
                self._tokens.pop(session_id, None)
            return json.loads(body.decode("utf-8"))
        raise DeviceSessionRefused(f"unsupported dashboard console op: {op}", status=422, code="unknown_op")


_GUEST_BOOT_UUID_CMD = (
    "PATH=/var/jb/usr/sbin:/var/jb/sbin:/var/jb/usr/bin:/var/jb/bin:"
    "/iosbinpack64/usr/sbin:/iosbinpack64/sbin:/usr/sbin:/usr/bin:/sbin:/bin "
    "sysctl -n kern.bootsessionuuid"
)


def read_guest_boot_uuid(
    target: DeviceTarget,
    *,
    shell_runner: Callable[..., Any] | None = None,
) -> str:
    """Read and validate the guest Darwin kern.bootsessionuuid over UDS."""
    runner = shell_runner or run_shell
    try:
        result = runner(
            _GUEST_BOOT_UUID_CMD,
            timeout_ms=5000,
            sock_path=str(target.socket),
            protocol=target.protocol,
        )
    except (OSError, ShellError, RuntimeError) as exc:
        raise RuntimeError(f"cannot read guest boot UUID for {target.rig_id}: {exc}") from exc
    if getattr(result, "code", 1) != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
        raise RuntimeError(f"cannot read guest boot UUID for {target.rig_id}: exit {result.code} {detail}".strip())
    raw = str(getattr(result, "stdout", "") or "").strip()
    try:
        return str(UUID(raw))
    except ValueError as exc:
        raise RuntimeError(f"invalid guest boot UUID for {target.rig_id}: {raw!r}") from exc


def ensure_usb_trust_checked(
    target: DeviceTarget,
    client: DeviceSessions,
    *,
    wait_s: float = 25.0,
    boot_uuid: str | None = None,
    control_http: str | None = None,
    wait_for_device_fn: Callable[..., None] = wait_for_device,
    dismiss_fn: Callable[..., dict] = dismiss,
    read_boot_uuid_fn: Callable[[DeviceTarget], str] = read_guest_boot_uuid,
) -> dict:
    """Check and decline an iOS USB trust prompt at most once per guest boot session."""
    if not 0 <= wait_s <= 60:
        raise ValueError("usb_trust_wait must be between 0 and 60 seconds")
    resolved_boot = str(UUID(boot_uuid)) if boot_uuid is not None else read_boot_uuid_fn(target)
    receipt_root = target.runtime_dir / "usb-trust" / resolved_boot
    complete_file = receipt_root / "complete"
    if complete_file.is_file() and complete_file.read_text(encoding="utf-8").strip():
        return {
            "status": "already_checked",
            "boot_uuid": resolved_boot,
            "complete": str(complete_file),
            "result_path": complete_file.read_text(encoding="utf-8").strip(),
        }
    if control_http:
        wait_for_device_fn(control_http, target)
    attempt_dir = receipt_root / f"attempt.{uuid4().hex[:8]}"
    evidence = attempt_dir / "evidence"
    result = dismiss_fn(client, target.rig_id, evidence, wait_s=wait_s)
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp_complete = complete_file.with_name(complete_file.name + ".tmp")
    tmp_complete.write_text(f"{result_path}\n", encoding="utf-8")
    tmp_complete.replace(complete_file)
    return {
        **result,
        "boot_uuid": resolved_boot,
        "evidence": str(evidence),
        "result_path": str(result_path),
    }


def _parse_basic_auth(raw: str | None) -> tuple[str, str] | None:
    if not raw:
        return None
    user, sep, password = raw.split(",", 1)[0].partition(":")
    if not sep:
        return None
    return user.strip(), password.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig", required=True)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--control-http", default=control_http_base_url())
    parser.add_argument(
        "--dashboard-http",
        default=None,
        help="use a local single-VM dashboard URL (http://127.0.0.1:8000) instead of Control Plane",
    )
    parser.add_argument("--wait", type=float, default=15.0)
    args = parser.parse_args()
    if not 0 <= args.wait <= 60:
        parser.error("--wait must be between 0 and 60 seconds")
    if args.dashboard_http:
        if args.target:
            target = DeviceTarget.load(args.target)
            if args.rig != target.rig_id:
                parser.error("--rig differs from --target")
        client: DeviceSessions = DashboardDeviceSessions(
            args.dashboard_http,
            auth=_parse_basic_auth(os.getenv("AUTOPHONE_AUTH")),
        )
    else:
        if args.target:
            target = DeviceTarget.load(args.target)
            if args.rig != target.rig_id:
                parser.error("--rig differs from --target")
            wait_for_device(args.control_http, target)
        client = RemoteDeviceSessions(args.control_http)
    result = dismiss(client, args.rig, args.output, wait_s=args.wait)
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
