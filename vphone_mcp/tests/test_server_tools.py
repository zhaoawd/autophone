"""Unit tests for vphone_mcp FastMCP server tool classification and lock dispatch."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from vphone_mcp import server
from vphone_mcp.client import PROTOCOL_UPSTREAM_V1, VPhoneClient
from vphone_mcp.coordination import (
    ControlPlaneUnavailable,
    LockRequiredError,
    ScreenLockBusy,
)
from tests.test_upstream_v1 import default_upstream_caps, make_jpeg_b64, uds_endpoint


class TestServerTools(unittest.TestCase):
    def test_every_registered_tool_is_classified_with_matching_annotations(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(server.READ_ONLY_TOOLS & server.MUTATING_TOOLS, frozenset())
        self.assertEqual(names, server.READ_ONLY_TOOLS | server.MUTATING_TOOLS)

        for tool in tools:
            self.assertIsNotNone(tool.annotations, f"missing ToolAnnotations on {tool.name}")
            if tool.name in server.READ_ONLY_TOOLS:
                self.assertTrue(
                    tool.annotations.readOnlyHint,
                    f"read-only tool {tool.name} must have readOnlyHint=True",
                )
            else:
                self.assertFalse(
                    tool.annotations.readOnlyHint,
                    f"mutating tool {tool.name} must have readOnlyHint=False",
                )

    def test_open_app_holds_single_lock_and_upstream_v1_followup_screenshot(self) -> None:
        img = make_jpeg_b64(430, 932)
        owner_ids_seen: list[str] = []

        with tempfile.TemporaryDirectory(prefix="vmcp-srv-") as state_dir:
            lock_dir = Path(state_dir) / "runs" / ".locks"

            def handler(msg: dict[str, Any]) -> list[bytes]:
                state_file = lock_dir / "rig-a.json"
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    if state.get("locked") and state.get("owner_id"):
                        owner_ids_seen.append(str(state["owner_id"]))
                if msg["t"] == "screenshot":
                    return [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")]
                # upstream_v1 HID command returning ok=True WITHOUT inline image!
                return [b'{"ok":true}\n']

            with uds_endpoint(handler, capabilities=default_upstream_caps(), max_requests=20) as (
                sock_path,
                sent,
                _,
            ):
                env = {
                    "VPHONE_SOCK": sock_path,
                    "AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1",
                    "AUTOPHONE_STATE_DIR": state_dir,
                    "VPHONE_DEVICE": "rig-a",
                }
                with patch.dict(os.environ, env, clear=True), patch("vphone_mcp.server.time.sleep"):
                    VPhoneClient.reset_geometry()
                    result = server.open_app("Settings")
                    self.assertEqual(len(result), 1)
                    self.assertEqual(result[0].data, img)

                # Confirm key("home"), tap(...), and follow-up screenshot all ran under ONE lease.
                self.assertEqual(
                    [m["t"] for m in sent],
                    ["capabilities", "key", "capabilities", "tap", "capabilities", "screenshot"],
                )
                self.assertGreaterEqual(len(owner_ids_seen), 3)
                self.assertEqual(len(set(owner_ids_seen)), 1)

                # Lock released after tool completes.
                final_state = json.loads((lock_dir / "rig-a.json").read_text(encoding="utf-8"))
                self.assertFalse(final_state["locked"])

    def test_error_code_preserved_and_lock_released_on_tool_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vmcp-srv-err-") as state_dir:
            lock_dir = Path(state_dir) / "runs" / ".locks"
            err_body = (
                json.dumps({
                    "ok": False,
                    "error": "Location Services are off",
                    "guest_error": {"code": "location_services_off", "message": "Location Services are off"},
                })
                + "\n"
            ).encode("utf-8")

            with uds_endpoint([err_body], capabilities=default_upstream_caps()) as (sock_path, _, _):
                env = {
                    "VPHONE_SOCK": sock_path,
                    "AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1",
                    "AUTOPHONE_STATE_DIR": state_dir,
                    "VPHONE_DEVICE": "rig-a",
                }
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(RuntimeError, r"\[location_services_off\]"):
                        server.set_location(31.23, 121.47)

                final_state = json.loads((lock_dir / "rig-a.json").read_text(encoding="utf-8"))
                self.assertFalse(final_state["locked"])

    def test_get_capabilities_and_read_only_vs_mutating_enforcement(self) -> None:
        img = make_jpeg_b64(430, 932)

        def handler(msg: dict[str, Any]) -> list[bytes]:
            if msg["t"] == "screenshot":
                return [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")]
            if msg["t"] == "rpc" and msg["method"] == "apps.list":
                return [b'{"ok":true,"result":{"apps":[{"bundle_id":"com.apple.Preferences"}]}}\n']
            return [b'{"ok":true}\n']

        with uds_endpoint(handler, capabilities=default_upstream_caps(), max_requests=20) as (
            sock_path,
            _,
            _,
        ):
            env = {
                "VPHONE_SOCK": sock_path,
                "AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1",
                "VPHONE_MCP_REQUIRE_LOCK": "1",
            }
            with patch.dict(os.environ, env, clear=True):
                VPhoneClient.reset_geometry()
                # Read-only tools succeed even when VPHONE_MCP_REQUIRE_LOCK=1 in standalone mode.
                caps = server.get_capabilities()
                self.assertEqual(caps["protocol"], PROTOCOL_UPSTREAM_V1)
                self.assertEqual(caps["coordination"]["mode"], "standalone")
                self.assertTrue(caps["capabilities"]["available"])

                apps = server.list_apps()
                self.assertEqual(apps["apps"][0]["bundle_id"], "com.apple.Preferences")

                shot = server.screenshot()
                self.assertEqual(shot[0].data, img)

                # Mutating tools fail closed with LockRequiredError.
                for call in (
                    lambda: server.tap(10, 20),
                    lambda: server.go_home(),
                    lambda: server.type_text("hi"),
                    lambda: server.launch_app("com.apple.Preferences"),
                    lambda: server.terminate_app("com.apple.Preferences"),
                    lambda: server.set_location(31.23, 121.47),
                    lambda: server.stop_location(),
                ):
                    with self.assertRaises(LockRequiredError):
                        call()

        # Unreachable Control Plane rejects mutating tool via server cleanly.
        with patch.dict(
            os.environ,
            {
                "VPHONE_SOCK": "/tmp/never-touched.sock",
                "AUTOPHONE_CONTROL_PLANE_URL": "http://127.0.0.1:1",
                "VPHONE_DEVICE": "rig-a",
                "VPHONE_MCP_HTTP_TIMEOUT_S": "0.5",
            },
            clear=True,
        ):
            with self.assertRaises(ControlPlaneUnavailable):
                server.go_home()

        # Local ScreenLock contention rejects server mutating tool with ScreenLockBusy.
        with tempfile.TemporaryDirectory(prefix="vmcp-srv-busy-") as state_dir:
            from vphone_mcp.coordination import LocalScreenLock

            lock_dir = Path(state_dir) / "runs" / ".locks"
            lease = LocalScreenLock(lock_dir).acquire_lease(device="rig-a", owner_id="RUN-busy")
            try:
                with patch.dict(
                    os.environ,
                    {
                        "VPHONE_SOCK": "/tmp/never-touched.sock",
                        "AUTOPHONE_STATE_DIR": state_dir,
                        "VPHONE_DEVICE": "rig-a",
                    },
                    clear=True,
                ):
                    with self.assertRaises(ScreenLockBusy):
                        server.tap(10, 20)
            finally:
                lease.release()


if __name__ == "__main__":
    unittest.main()
