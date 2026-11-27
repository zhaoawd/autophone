"""Read-only static and runtime UI discovery for an installed iOS app.

Static IPA analysis produces candidates.  Runtime AX is the source of truth
for the controller and elements actually visible in one application state.
"""
from __future__ import annotations

import json
import plistlib
import re
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autophone.device.uds import request, save_image
from autophone.device.ax_session import AxSession
from autophone.device.ocr_target import find_text, run_ocr
from autophone.device.shell import run_shell

# An ``app_launch`` issued while SpringBoard is still animating (Home dismissal,
# or the app-death animation after ``app_terminate``) is swallowed — yet vphoned
# still answers ok=true with a valid pid, because the process is alive either
# way.  A pid check therefore cannot see the miss, and every later frame scans
# the iOS desktop.  Live A/B on 2026-07-27, three runs per step: 0s →
# HOME/HOME/HOME, 0.5s → APP/APP/APP, 1.0s → APP/APP/APP.  Take 1.0s for margin.
HOME_ANIMATION_SETTLE_S = 1.0

_ASCII = re.compile(rb"[\x20-\x7e]{4,}")
_CLASS = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*)(?:ViewController|VC|Cell|View|Model)$")
_ENDPOINT = re.compile(r"/(?:api/)?(?:order)/[^\s\"']+", re.I)


def resolve_app_identifier(
    app: str,
    *,
    profile_loader: Callable[[str], Any] | None = None,
) -> str:
    """Resolve an app-profile key to its bundle ID, preserving raw bundle IDs.

    Passing a profile key such as ``food_ordering`` straight to Frida's
    application list silently means "not running" although the bundle is
    alive.  Unknown values remain unchanged so the generic tool remains useful
    for ad-hoc bundle IDs and unit fixtures.

    ``profile_loader`` 由调用方提供：设备层不认识资产层（BL-003 判据 A），所以
    「App Profile 键 → bundle id」这一步要由上层传进来。不传时按 raw bundle id
    处理、原样返回——库内调用方传的都已是 bundle id；会传 Profile 键的调用方须显式传入 loader。
    """
    value = str(app or "").strip()
    if not value or "." in value or profile_loader is None:
        return value
    try:
        profile = profile_loader(value)
        bundle_id = str(getattr(profile, "bundle_id", "") or "")
        return bundle_id or value
    except Exception:  # noqa: BLE001 - a caller may have passed a raw alias
        return value


def _strings(blob: bytes) -> list[str]:
    # Mach-O strings are usually NUL-separated, but fixtures and some linker
    # sections contain several tokens in one printable run.  Split whitespace
    # so class/route recognition remains token-based rather than accidental.
    return sorted({token for m in _ASCII.finditer(blob)
                   for token in m.group().decode("ascii", "ignore").split()})


def index_ipa(ipa_path: str | Path) -> dict[str, Any]:
    """Return UI/route candidates from an IPA without extracting it to disk."""
    ipa = Path(ipa_path)
    with zipfile.ZipFile(ipa) as archive:
        names = archive.namelist()
        info_name = next((name for name in names if name.endswith(".app/Info.plist")), "")
        if not info_name:
            raise ValueError(f"IPA has no app Info.plist: {ipa}")
        info = plistlib.loads(archive.read(info_name))
        app_root = info_name.rsplit("/", 1)[0]
        executable = str(info.get("CFBundleExecutable") or "")
        binary_name = f"{app_root}/{executable}"
        binary = archive.read(binary_name) if executable and binary_name in names else b""

    strings = _strings(binary)
    resources = sorted({
        Path(name).name for name in names
        if (name.endswith(".nib") or ".storyboardc/" in name)
        and Path(name).name not in {"Info.plist", "runtime.nib", "objects-12.3+.nib"}
    })
    # Objective-C/Swift class symbols conventionally start uppercase.  Requiring
    # that avoids treating selector/property fragments such as `currentVC` as
    # page candidates.
    classes = sorted({text for text in strings if _CLASS.fullmatch(text) and text[:1].isupper()})
    endpoints = sorted({match.group() for text in strings for match in _ENDPOINT.finditer(text)})
    return {
        "kind": "autophone.static-ui-index/v1",
        "ipa": str(ipa),
        "bundle_id": str(info.get("CFBundleIdentifier") or ""),
        "display_name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or ""),
        "executable": executable,
        "candidates": {
            "controllers": [text for text in classes if text.endswith(("VC", "ViewController"))],
            "cells": [text for text in classes if text.endswith("Cell")],
            "models": [text for text in classes if text.endswith("Model")],
            "resources": resources,
            "endpoints": endpoints,
        },
    }


def summarise_ax(raw: dict[str, Any]) -> dict[str, Any]:
    """Make raw AX evidence compact while preserving semantic button state."""
    nodes = raw.get("nodes") or []
    labels = Counter(str(node.get("label") or "") for node in nodes if node.get("label"))
    roles = Counter(str(node.get("role") or "unknown") for node in nodes)
    return {
        "view_controller": str(raw.get("view_controller") or ""),
        "window_class": str(raw.get("window_class") or ""),
        "node_count": len(nodes),
        "roles": dict(sorted(roles.items())),
        "labels": sorted(labels),
        "duplicate_labels": sorted(label for label, count in labels.items() if count > 1),
        "buttons": [
            {
                "label": str(node.get("label") or ""),
                "enabled": bool(node.get("enabled", True)),
                "identifier": str(node.get("identifier") or ""),
                "class": str(node.get("class") or ""),
                "path": str(node.get("path") or ""),
            }
            for node in nodes if node.get("role") == "button"
        ],
    }


def capture_runtime_snapshot(
    app: str,
    out_dir: str | Path,
    *,
    session_factory: Callable[..., Any] = AxSession,
    request_fn: Callable[..., Any] = request,
) -> dict[str, Any]:
    """Capture one AX tree and screenshot without dispatching any UI event."""
    app = resolve_app_identifier(app)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # UDS file_get's host-side `save` is resolved by vphone-cli, not this
    # Python process.  Give it an absolute path so callers may safely use a
    # normal relative run directory.
    ax_path = (out / "ax.json").resolve()
    with session_factory(app, out=str(ax_path)) as session:
        session.refresh()
    raw = json.loads(ax_path.read_text(encoding="utf-8"))
    response, _ = request_fn({"t": "screenshot"})
    if not response.get("ok"):
        raise RuntimeError(f"screenshot failed: {response}")
    screenshot = save_image(response, out / "screen.jpg")
    evidence = {
        "kind": "autophone.runtime-ui-snapshot/v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "app": app,
        "ax_file": ax_path.name,
        "screenshot": screenshot.name,
        "ax": summarise_ax(raw),
    }
    (out / "snapshot.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


def tap_exact_ax(
    app: str,
    label: str,
    *,
    role: str = "button",
    out_dir: str | Path,
    session_factory: Callable[..., Any] = AxSession,
    request_fn: Callable[..., Any] = request,
    settle_s: float = 0.8,
) -> dict[str, Any]:
    """Tap exactly one enabled AX node, refusing substring and duplicate matches.

    This is intentionally a narrow discovery adapter: callers must name a safe
    navigation control, while the adapter owns AX refresh, exact matching and
    points-to-native mapping.  Lifecycle buttons are never guessed.
    """
    app = resolve_app_identifier(app)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ax_path = (out / "before-tap.ax.json").resolve()
    with session_factory(app, out=str(ax_path)) as session:
        dump = session.refresh()
    matches = [node for node in dump.nodes if node.label == label and node.role == role and node.enabled]
    if len(matches) != 1:
        raise LookupError(f"safe AX tap requires exactly one enabled {role} label={label!r}; got {len(matches)}")
    target = matches[0]
    response, _ = request_fn({"t": "tap", "x": target.native_x, "y": target.native_y})
    if not response.get("ok"):
        raise RuntimeError(f"tap {label!r} failed: {response}")
    time.sleep(settle_s)
    evidence = {
        "kind": "autophone.ax-navigation/v1",
        "app": app,
        "label": label,
        "role": role,
        "native": [target.native_x, target.native_y],
        "path": target.path,
    }
    (out / "tap.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


def relaunch_for_discovery(
    app: str,
    *,
    out_dir: str | Path,
    request_fn: Callable[..., Any] = request,
    shell_runner: Callable[..., Any] = run_shell,
    ready_markers: tuple[str, ...] = (),
    ocr_runner: Callable[[str | Path], dict[str, Any]] = run_ocr,
    ready_timeout_s: float = 20.0,
    settle_s: float = 2.0,
    home_settle_s: float = HOME_ANIMATION_SETTLE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Cold-relaunch only the named app to reset navigation for discovery.

    Wake/unlock the display first, then use repaired vphoned's native lifecycle
    commands.  A background AX tree can remain readable while the display is
    asleep; touching against that state previously produced a false "ready"
    followed by ignored taps.  Native app_terminate/app_launch also verifies
    the exact bundle instead of relying on kill + uiopen foreground ambiguity.

    Both the Home key and ``app_terminate`` hand SpringBoard an animation, and a
    launch that lands inside it is dropped while still answering ok=true with a
    live pid — so the ``home_settle_s`` gap before the launch is load-bearing,
    not politeness (see :data:`HOME_ANIMATION_SETTLE_S`).

    ``shell_runner`` remains in the signature for callers pinned to the older
    seam, but lifecycle execution is intentionally native and fail-closed.
    """
    app = resolve_app_identifier(app)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    apps, _ = request_fn({"t": "app_list", "filter": "running"})
    pids = [int(row.get("pid") or 0) for row in apps.get("apps", [])
            if row.get("bundle_id") == app and int(row.get("pid") or 0) > 0]

    woke, _ = request_fn({"t": "key", "name": "home", "screen": False})
    if not woke.get("ok"):
        raise RuntimeError(f"failed to wake device before relaunching {app}: {woke}")

    if pids:
        terminated, _ = request_fn(
            {"t": "app_terminate", "bundle_id": app, "screen": False}
        )
        if not terminated.get("ok"):
            raise RuntimeError(f"native app_terminate failed for {app}: {terminated}")

    sleep(home_settle_s)
    launched, _ = request_fn({"t": "app_launch", "bundle_id": app, "screen": False})
    try:
        launch_pid = int(launched.get("pid") or 0)
    except (TypeError, ValueError):
        launch_pid = 0
    if not launched.get("ok") or launch_pid <= 0:
        raise RuntimeError(f"native app_launch failed for {app}: {launched}")
    sleep(settle_s)
    ready_marker = ""
    if ready_markers:
        deadline = time.monotonic() + max(0.0, ready_timeout_s)
        ready_shot = out / "ready.jpg"
        while time.monotonic() < deadline:
            response, _ = request_fn({"t": "screenshot"})
            if response.get("ok") and response.get("image"):
                try:
                    save_image(response, ready_shot)
                    ocr = ocr_runner(ready_shot)
                    ready_marker = next((marker for marker in ready_markers if find_text(ocr, marker, min_confidence=0.4)), "")
                    if ready_marker:
                        break
                except Exception:  # black/loading frame or an OCR transport failure
                    pass
            time.sleep(0.5)
        if not ready_marker:
            raise RuntimeError(f"{app} did not become visually ready; markers={list(ready_markers)!r}")
    evidence = {"kind": "autophone.discovery-relaunch/v1", "app": app,
                "terminated_pids": pids, "launch_pid": launch_pid,
                "ready_marker": ready_marker}
    (out / "relaunch.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence
